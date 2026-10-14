import os
import re
import math
import time
import logging
from datetime import datetime
import requests
from bs4 import BeautifulSoup, NavigableString
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import InvalidSessionIdException

from scraper.driver import get_driver
from scraper.utils import clean_text, clean_record
from config.settings import Config

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "2026-10-14-v6-dom-accurate"

# Robust row extraction: identifies result rows with detalle.do/certificaTramite links
# or numeric IDs, preventing header/breadcrumb false matches while accepting rows regardless of column order.
ROWS_JS = r"""
const out = [];
document.querySelectorAll('tr').forEach(tr => {
  const tds = Array.from(tr.children).filter(c => c.tagName === 'TD');
  if (tds.length < 4) return;

  const links = tds.map(td => {
    const a = td.querySelector('a');
    return a ? (a.href || '') : '';
  });

  const t = tds.map(td => (td.innerText || '').trim());

  let g = '';
  let o = '';

  for (const l of links) {
    const mg = l.match(/idGarantia=(\d+)/i);
    const mo = l.match(/idTramite=(\d+)/i);
    if (mg && !g) g = mg[1];
    if (mo && !o) o = mo[1];
  }

  if (!g || !o) {
    const numCells = t.filter(c => /^\d{6,}$/.test(c));
    if (numCells.length >= 2) {
      if (!g) g = numCells[0];
      if (!o) o = numCells[1];
    } else if (/^\d+$/.test(t[0]) && /^\d+$/.test(t[1])) {
      if (!g) g = t[0];
      if (!o) o = t[1];
    }
  }

  const hasDetailLink = links.some(l => l.includes('detalle.do') || l.includes('certificaTramite'));

  if ((g && o) || (hasDetailLink && tds.length >= 5)) {
    out.push({cells: t, links: links, g: g, o: o});
  }
});
return out;
"""

PAGE_INFO_RE = re.compile(r"MOSTRANDO\s+(\d+)\s+A\s+(\d+)\s+DE\s+(\d+)\s+REGISTROS")


class SessionExpired(Exception):
    pass


class RugExtractor:
    def __init__(self, s3_client=None, db_repo=None, debug_dir="debug_output",
                 output_dir="local_output", dry_run=None):
        logger.info(f"RugExtractor build: {EXTRACTOR_VERSION}")
        self.driver = get_driver()
        self.s3 = s3_client
        self.db = db_repo
        self.debug_dir = debug_dir
        self.output_dir = output_dir
        self.dry_run = Config.DRY_RUN if dry_run is None else dry_run
        self.is_authenticated = False
        self._sample_saved = False
        self._empty_field_reports = 0
        os.makedirs(self.debug_dir, exist_ok=True)
        if self.dry_run or not self.s3:
            os.makedirs(self.output_dir, exist_ok=True)
            if self.dry_run:
                logger.warning("DRY_RUN is ON: nothing is written to MongoDB or S3. Files go to local_output/.")
            else:
                logger.info("MongoDB is ACTIVE; AWS S3 paused (files saved to local_output/).")

    # ------------------------------------------------------------------ helpers
    def _save_debug_state(self, step_name: str):
        try:
            self.driver.save_screenshot(os.path.join(self.debug_dir, f"{step_name}.png"))
            with open(os.path.join(self.debug_dir, f"{step_name}.html"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            logger.info(f"Saved debug snapshot: {step_name} (URL: {self.driver.current_url})")
        except Exception as e:
            logger.warning(f"Could not save debug state for {step_name}: {e}")

    def authenticate(self):
        logger.info("Authenticating with RUG Portal...")
        self.driver.get("https://rug.economia.gob.mx/Rug/home/login.do?")
        try:
            user_input = WebDriverWait(self.driver, 25).until(
                EC.presence_of_element_located((By.ID, "idUserFake"))
            )
            user_input.clear()
            user_input.send_keys(Config.RUG_EMAIL)
            pwd_input = self.driver.find_element(By.ID, "j_password")
            pwd_input.clear()
            pwd_input.send_keys(Config.RUG_PASSWORD)
            self.driver.find_element(By.ID, "Ingresar").click()
            time.sleep(5)

            curr_url = self.driver.current_url.lower()
            page_src = self.driver.page_source.lower()
            if "error=true" in curr_url or "incorrecto" in page_src and "j_security_check" in page_src:
                self._save_debug_state("01_login_failed")
                raise Exception("RUG Portal rejected credentials. Verify RUG_EMAIL / RUG_PASS in .env.")

            self.is_authenticated = True
            logger.info(f"Authenticated successfully. Current URL: {self.driver.current_url}")
            self._save_debug_state("01_authenticated_success")
        except Exception as e:
            self._save_debug_state("01_auth_exception")
            logger.error(f"Authentication error: {e}")
            raise

    def check_saturation(self) -> bool:
        page_src = self.driver.page_source.lower()
        for indicator in ["sitio saturado", "portal saturado", "capacidad maxima",
                          "intente mas tarde", "servicio no disponible", "error 503", "service unavailable"]:
            if indicator in page_src:
                logger.warning(f"Portal saturation detected ({indicator}). Sleeping {Config.SATURATION_SLEEP_SECONDS}s per TS Section V.")
                self._save_debug_state("saturation_detected")
                time.sleep(Config.SATURATION_SLEEP_SECONDS)
                return True
        return False

    def _get_http_session(self) -> requests.Session:
        session = requests.Session()
        for cookie in self.driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])
        ua = self.driver.execute_script("return navigator.userAgent;")
        session.headers.update({
            "User-Agent": ua,
            "Referer": "https://rug.economia.gob.mx/Rug/home/busqueda.do",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        })
        return session

    def _fetch_with_retry(self, session, url, what, attempts=1, timeout=(5, 15)):
        """GET with retries. Returns None if timed out or failed."""
        for attempt in range(1, attempts + 1):
            try:
                resp = session.get(url, timeout=timeout)
                if resp.status_code == 200:
                    return resp
                logger.warning(f"{what}: HTTP {resp.status_code} (attempt {attempt}/{attempts})")
            except requests.RequestException as e:
                logger.warning(f"{what}: {type(e).__name__} (attempt {attempt}/{attempts})")
            if attempt < attempts:
                time.sleep(2 * attempt)
        return None

    def _fetch_detail_html(self, session, url) -> str:
        resp = self._fetch_with_retry(session, url, "detail page", attempts=2, timeout=(5, 25))
        if resp is None:
            return ""
        if "j_security_check" in resp.text:
            raise SessionExpired("Detail page returned the login form.")
        return resp.text

    def _fetch_pdf(self, session, url) -> bytes:
        resp = self._fetch_with_retry(session, url, "certificate PDF", attempts=1, timeout=(5, 15))
        if resp is None:
            return b""
        if resp.content.startswith(b"%PDF"):
            return resp.content
        logger.warning(f"certificate PDF: response is not a PDF (len={len(resp.content)}, starts={resp.content[:20]!r})")
        return b""

    def _extract_detail_fields(self, html: str) -> dict:
        if not html:
            return {
                "fecha_inscripcion_detalle": "",
                "fecha_ultimo_asiento": "",
                "acreedor": "",
                "vigencia": "",
                "deudor": [{"nombre_razon_denominacion": "", "tipo_persona": ""}],
                "tipo_garantia_mobiliaria": "",
                "fecha": "",
                "monto_maximo_garantizado": "",
                "bienes_objeto": "",
                "descripcion_bienes": "",
                "datos_instrumento_publico_origen": "",
                "terminos_condiciones": "",
            }

        soup = BeautifulSoup(html, 'html.parser')
        res = {}

        # 1. Summary tables: .encabezadoTablaResumen followed by .cuerpo1TablaResumen
        for table in soup.find_all('table'):
            headers = [clean_text(td.get_text()) for td in table.find_all('td', class_='encabezadoTablaResumen')]
            values = [clean_text(td.get_text()) for td in table.find_all('td', class_='cuerpo1TablaResumen')]
            if headers and values:
                for h, v in zip(headers, values):
                    if 'FECHA DE INSCRIPCION' in h and 'fecha_inscripcion_detalle' not in res:
                        res['fecha_inscripcion_detalle'] = v
                    elif 'ULTIMO ASIENTO' in h and 'fecha_ultimo_asiento' not in res:
                        res['fecha_ultimo_asiento'] = v
                    elif 'NOMBRE DEL ACREEDOR' in h and 'acreedor' not in res:
                        res['acreedor'] = v

        # Fallback for acreedor
        if not res.get('acreedor'):
            ac_tag = soup.find(string=re.compile(r'nombre\s+del\s+acreedor', re.I))
            if ac_tag:
                t = ac_tag.find_parent('table')
                if t:
                    c = t.find(class_='cuerpo1TablaResumen')
                    if c:
                        res['acreedor'] = clean_text(c.get_text())

        # 2. Vigencia: e.g. <span class="textoGeneralRojo">Vigencia:</span> 48 Meses
        sp = soup.find(lambda t: t.name == 'span' and 'textoGeneralRojo' in t.get('class', []) and 'vigencia' in t.get_text().lower())
        if sp and sp.parent:
            texts = [s.strip() for s in sp.parent.children if isinstance(s, NavigableString) and s.strip()]
            res['vigencia'] = clean_text(' '.join(texts))
        else:
            vig_match = re.search(r'Vigencia\s*:\s*([0-9]+\s+[A-Za-z]+)', html, re.I)
            res['vigencia'] = clean_text(vig_match.group(1)) if vig_match else ''

        # 3. Deudor(es): table#mytabledaD
        deudores = []
        d_table = soup.find('table', id='mytabledaD') or soup.find('table', class_='mytabledaD')
        if d_table:
            tbody = d_table.find('tbody')
            rows = tbody.find_all('tr') if tbody else d_table.find_all('tr')[1:]
            for r in rows:
                cols = [clean_text(td.get_text()) for td in r.find_all('td')]
                if cols and len(cols) >= 1 and cols[0] and cols[0] != '//////':
                    deudores.append({
                        'nombre_razon_denominacion': cols[0],
                        'tipo_persona': cols[1] if len(cols) > 1 else ''
                    })
        if not deudores:
            deudores = [{'nombre_razon_denominacion': '', 'tipo_persona': ''}]
        res['deudor'] = deudores

        # 4. Acto o Contrato fields helper
        def get_field_after_label(label_pattern):
            for div in soup.find_all('div', class_='textoGeneralRojo'):
                if re.search(label_pattern, div.get_text(), re.I):
                    sib = div.find_next_sibling(lambda t: t.name in ['div', 'span', 'table'])
                    if sib:
                        ta = sib.find('textarea')
                        if ta:
                            return clean_text(ta.get_text())
                        cn = sib.find(class_='contenidoNota')
                        if cn:
                            return clean_text(cn.get_text())
                        st = clean_text(sib.get_text())
                        if st:
                            return st
                    p = div.parent
                    if p and p.name == 'div':
                        psib = p.find_next_sibling(lambda t: t.name in ['div', 'span'])
                        if psib:
                            cn = psib.find(class_='contenidoNota')
                            if cn:
                                return clean_text(cn.get_text())
                            st = clean_text(psib.get_text())
                            if st:
                                return st
            return ''

        # Tipo de Garantia Mobiliaria
        res['tipo_garantia_mobiliaria'] = get_field_after_label(r'tipo\s+de\s+garant')

        # Fecha celebracion / surgimiento
        res['fecha'] = get_field_after_label(r'fecha\s+de\s+(?:celebraci|surgimiento)')

        # Monto Maximo Garantizado
        res['monto_maximo_garantizado'] = get_field_after_label(r'monto')

        # Bienes Objeto
        bo_div = soup.find(lambda t: t.name == 'div' and t.get('class') == ['textoGeneralRojo'] and re.search(r'bienes\s+muebles\s+objeto', t.get_text(), re.I) and not re.search(r'descripci', t.get_text(), re.I))
        if bo_div:
            sib = bo_div.find_next_sibling('div')
            if sib:
                cn = sib.find(class_='contenidoNota')
                res['bienes_objeto'] = clean_text(cn.get_text()) if cn else clean_text(sib.get_text())
            else:
                res['bienes_objeto'] = ''
        else:
            res['bienes_objeto'] = ''

        # Descripcion Bienes
        ta_bienes = soup.find('textarea', id='tiposbienes') or soup.find('textarea', attrs={'name': 'tiposbienes'})
        res['descripcion_bienes'] = clean_text(ta_bienes.get_text()) if ta_bienes else ''

        # Datos Instrumento Publico
        res['datos_instrumento_publico_origen'] = get_field_after_label(r'instrumento')

        # Terminos y Condiciones
        ta_term = soup.find('textarea', id='otrosgarantia') or soup.find('textarea', id='otrosterminos')
        res['terminos_condiciones'] = clean_text(ta_term.get_text()) if ta_term else ''

        default_keys = [
            'fecha_inscripcion_detalle', 'fecha_ultimo_asiento', 'acreedor', 'vigencia',
            'tipo_garantia_mobiliaria', 'fecha', 'monto_maximo_garantizado',
            'bienes_objeto', 'descripcion_bienes', 'datos_instrumento_publico_origen', 'terminos_condiciones'
        ]
        for k in default_keys:
            res.setdefault(k, '')

        return res

    # --------------------------------------------------------------- navigation
    def _navigate_to_search(self):
        existing = self.driver.find_elements(By.ID, "rfcOtorgante")
        if existing:
            logger.info("Search inputs already present on current page.")
            return
        logger.info("Navigating to busqueda.do...")
        self.driver.get("https://rug.economia.gob.mx/Rug/home/busqueda.do?")
        time.sleep(3)
        self.check_saturation()

    def _page_info(self):
        """Returns (first, last, total) from 'Mostrando X a Y de N registros', or None."""
        try:
            text = self.driver.execute_script("return document.body.innerText || ''") or ""
        except Exception:
            return None
        m = PAGE_INFO_RE.search(text.upper())
        return tuple(int(x) for x in m.groups()) if m else None

    def _read_rows(self):
        return self.driver.execute_script(ROWS_JS) or []

    def _go_next_page(self, prev_info) -> bool:
        buttons = [b for b in self.driver.find_elements(
            By.XPATH, "//a[contains(normalize-space(.), 'Siguiente') or contains(@title, 'Siguiente')]"
        ) if b.is_displayed()]
        if not buttons:
            return False
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", buttons[0])
        buttons[0].click()
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            info = self._page_info()
            if info and prev_info and info[:2] != prev_info[:2]:
                return True
        logger.warning("Page indicator did not change after clicking 'Siguiente'. Stopping pagination.")
        return False

    # ------------------------------------------------------------- persistence
    def _persist_files(self, id_searched, g, o, list_html, detail_html, pdf_bytes):
        """Dry run or S3 disabled: local files. Normal run with S3: upload to S3."""
        if self.dry_run or not self.s3:
            folder = os.path.abspath(os.path.join(self.output_dir, id_searched, f"{g}_{o}"))
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "list.html"), "w", encoding="utf-8") as f:
                f.write(list_html)
            with open(os.path.join(folder, "detail.html"), "w", encoding="utf-8") as f:
                f.write(detail_html)
            source_pdf = ""
            if pdf_bytes:
                with open(os.path.join(folder, "certificate.pdf"), "wb") as f:
                    f.write(pdf_bytes)
                source_pdf = f"file:///{folder}/certificate.pdf".replace("\\", "/")
            return f"file:///{folder}/detail.html".replace("\\", "/"), source_pdf

        self.s3.upload_file_content(list_html, id_searched, g, o, "list.html")
        source_html = self.s3.upload_file_content(detail_html, id_searched, g, o, "detail.html")
        source_pdf = ""
        if pdf_bytes:
            source_pdf = self.s3.upload_file_content(pdf_bytes, id_searched, g, o, "certificate.pdf")
        return source_html, source_pdf

    # -------------------------------------------------------------- extraction
    def _submit_search(self, target, is_curp):
        input_id = "curpOtorgante" if is_curp else "rfcOtorgante"
        inputs = self.driver.find_elements(By.ID, input_id)
        if not inputs:
            self._save_debug_state("error_no_search_input")
            raise Exception(f"Search input '{input_id}' not found on {self.driver.current_url}")
        box = inputs[0]
        box.clear()
        box.send_keys(target)
        logger.info(f"Typed {target} into #{input_id}")

        buttons = self.driver.find_elements(
            By.XPATH,
            "//input[@value='Buscar' or contains(@value, 'uscar')] | //button[contains(., 'Buscar')]"
        )
        if buttons:
            buttons[0].click()
            logger.info("Clicked 'Buscar'.")
        else:
            box.submit()
            logger.info("Submitted search via form submit().")

    def _wait_for_results(self, timeout=35) -> bool:
        """Polls until search results load or 'No se encontraron resultados' is displayed."""
        logger.info(f"Waiting up to {timeout}s for search results to load...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.check_saturation():
                return False
            page_src = self.driver.page_source.upper()
            if "NO SE ENCONTRARON RESULTADOS" in page_src or "0 REGISTROS" in page_src:
                logger.info("Search returned: 0 results / no records.")
                return True
            if self._page_info() is not None:
                logger.info(f"Detected page indicator: {self._page_info()}")
                return True
            rows = self._read_rows()
            if rows:
                logger.info(f"Detected {len(rows)} result rows on page.")
                return True
            time.sleep(2)
        logger.warning(f"Timeout ({timeout}s) waiting for results to appear.")
        return False

    def extract(self, target_curp_rfc: str, run_id: str = None) -> list:
        target = clean_text(target_curp_rfc)
        is_curp = (len(target) == 18)
        logger.info(f"Starting extraction for {'CURP' if is_curp else 'RFC'}: {target}")

        if not self.is_authenticated:
            self.authenticate()

        use_db = bool(self.db) and not self.dry_run

        if use_db and self.db.is_id_scraped_today(target):
            logger.info(f"Memory check: {target} already completed today. Skipping (TS Section II).")
            if run_id:
                self.db.update_run_log(run_id, {"status": "done", "error": "Skipped: already extracted today"})
            return []

        self._navigate_to_search()
        self._submit_search(target, is_curp)
        self._wait_for_results(timeout=35)
        self.check_saturation()
        self._save_debug_state(f"03_results_{target}")

        page_text = self.driver.page_source.upper()
        if "NO SE ENCONTRARON RESULTADOS" in page_text:
            logger.info(f"No results for {target}.")
            if use_db and run_id:
                self.db.update_run_log(run_id, {"status": "no_results"})
            return []

        http_session = self._get_http_session()
        records, seen = [], set()
        failed_keys = []
        consecutive_session_fail = 0
        total_expected, max_pages, page_num = None, 60, 1

        while True:
            info = self._page_info()
            if info and total_expected is None:
                total_expected = info[2]
                per_page = max(info[1] - info[0] + 1, 1)
                max_pages = math.ceil(total_expected / per_page) + 3
                logger.info(f"Site reports {total_expected} records, {per_page} per page (~{math.ceil(total_expected / per_page)} pages).")

            list_html = self.driver.page_source
            rows = self._read_rows()
            if not rows:
                time.sleep(3)
                rows = self._read_rows()
            logger.info(f"Page {page_num}: {len(rows)} real result rows (indicator: {info}).")

            if not rows:
                self._save_debug_state(f"error_no_rows_page_{page_num}")
                break

            for row in rows:
                c, links = row["cells"], row["links"]
                g = row.get("g") or (c[0] if len(c) > 0 and c[0].isdigit() else "")
                o = row.get("o") or (c[1] if len(c) > 1 and c[1].isdigit() else "")
                if not (g and o):
                    digits = [val for val in c if val.isdigit() and len(val) >= 5]
                    if len(digits) >= 2:
                        g, o = digits[0], digits[1]
                    else:
                        g, o = (c[0] if len(c) > 0 else ""), (c[1] if len(c) > 1 else "")

                key = (g, o)
                if key in seen:
                    logger.info(f"Page overlap: {g}/{o} already handled this run.")
                    continue
                seen.add(key)
                if use_db and run_id:
                    self.db.increment_run_log(run_id, {"rows_listed": 1})

                try:
                    if use_db and self.db.check_duplicate(target, g, o):
                        logger.info(f"Already complete in MongoDB, skipping {g}/{o}.")
                        if run_id:
                            self.db.increment_run_log(run_id, {"rows_duplicate": 1})
                        continue

                    link_garantia = next((l for l in links if "detalle.do" in l), "") or \
                        f"https://rug.economia.gob.mx/Rug/home/detalle.do?idGarantia={g}&idTramite={o}"
                    link_operacion = next((l for l in links if "certificaTramite" in l), "") or \
                        f"https://rug.economia.gob.mx/Rug/home/certificaTramite.do?idGarantia={g}&idTramite={o}"

                    detail_html = self._fetch_detail_html(http_session, link_garantia)
                    consecutive_session_fail = 0
                    if not detail_html:
                        raise RuntimeError("detail page could not be fetched")

                    pdf_bytes = self._fetch_pdf(http_session, link_operacion)
                    if not pdf_bytes:
                        logger.warning(f"Certificate PDF timed out or unavailable for {g}/{o}. Continuing with record save.")

                    if not self._sample_saved:
                        with open(os.path.join(self.debug_dir, f"04_detail_sample_{g}_{o}.html"), "w", encoding="utf-8") as f:
                            f.write(detail_html)
                        if pdf_bytes:
                            with open(os.path.join(self.debug_dir, f"04_certificate_sample_{g}_{o}.pdf"), "wb") as f:
                                f.write(pdf_bytes)
                        self._sample_saved = True
                        logger.info(f"Saved first real detail HTML sample for {g}/{o} into {self.debug_dir}/")

                    fields = self._extract_detail_fields(detail_html)

                    empty = [k for k, v in fields.items() if not v and k not in ['datos_instrumento_publico_origen', 'terminos_condiciones']]
                    if empty and self._empty_field_reports < 3:
                        self._empty_field_reports += 1
                        logger.warning(f"PARSER CHECK {g}/{o}: fields that came back EMPTY: {empty}")

                    source_html, source_pdf = self._persist_files(target, g, o, list_html, detail_html, pdf_bytes)

                    # Dynamic mapping of row columns based on garantia index
                    g_idx = next((i for i, val in enumerate(c) if val == g), -1)
                    if g_idx != -1 and len(c) > g_idx + 4:
                        tipo_operacion = c[g_idx + 2] if len(c) > g_idx + 2 else (c[2] if len(c) > 2 else "")
                        otorgante = c[g_idx + 3] if len(c) > g_idx + 3 else (c[3] if len(c) > 3 else "")
                        folio_electronico = c[g_idx + 4] if len(c) > g_idx + 4 else (c[4] if len(c) > 4 else "")
                        fecha_inscripcion = c[g_idx + 5] if len(c) > g_idx + 5 else (c[5] if len(c) > 5 else "")
                        tipo_bienes = c[g_idx + 6] if len(c) > g_idx + 6 else (c[6] if len(c) > 6 else "")
                    else:
                        tipo_operacion = c[2] if len(c) > 2 else ""
                        otorgante = c[3] if len(c) > 3 else ""
                        folio_electronico = c[4] if len(c) > 4 else ""
                        fecha_inscripcion = c[5] if len(c) > 5 else ""
                        tipo_bienes = c[6] if len(c) > 6 else ""

                    record = {
                        "CURP_consult": target if is_curp else "",
                        "RFC_consult": target if not is_curp else "",
                        "CURP_consulted": target if is_curp else "",
                        "RFC_consulted": target if not is_curp else "",
                        "otorgante": otorgante,
                        "numero_garantia": g,
                        "numero_operacion": o,
                        "tipo_operacion": tipo_operacion,
                        "folio_electronico": folio_electronico,
                        "fecha_inscripcion": fecha_inscripcion,
                        "fecha_inscripcion_detalle": fields["fecha_inscripcion_detalle"],
                        "tipo_bienes": tipo_bienes,
                        "link_garantia": link_garantia,
                        "link_operacion": link_operacion,
                        "fecha_ultimo_asiento": fields["fecha_ultimo_asiento"],
                        "acreedor": fields["acreedor"],
                        "vigencia": fields["vigencia"],
                        "deudor": fields["deudor"],
                        "acto_contrato_crea_garantia_mobiliaria": [{
                            "tipo_garantia_mobiliaria": fields["tipo_garantia_mobiliaria"],
                            "fecha": fields["fecha"],
                            "monto_maximo_garantizado": fields["monto_maximo_garantizado"],
                            "bienes_objeto": fields["bienes_objeto"],
                            "descripcion_bienes": fields["descripcion_bienes"],
                            "datos_instrumento_publico_origen": fields["datos_instrumento_publico_origen"],
                            "terminos_condiciones": fields["terminos_condiciones"],
                        }],
                        "origen": "REGISTRO UNICO DE GARANTIAS MOBILIARIAS",
                        "source_html": source_html,
                        "source_pdf": source_pdf,
                        "fecha_insercion": datetime.utcnow(),
                    }
                    record = clean_record(record)

                    if use_db:
                        saved = self.db.save_raw_data(record)
                        if run_id:
                            self.db.increment_run_log(run_id, {"rows_saved" if saved else "rows_duplicate": 1})

                    records.append(record)
                    logger.info(f"OK {len(records)}: garantia {g} / operacion {o}")
                    time.sleep(0.5)

                except InvalidSessionIdException:
                    logger.error("Chrome session died. Aborting this ID.")
                    self._save_debug_state("error_session_died")
                    raise
                except SessionExpired as se:
                    consecutive_session_fail += 1
                    failed_keys.append(key)
                    logger.error(f"{g}/{o}: {se} ({consecutive_session_fail}/3)")
                    if use_db and run_id:
                        self.db.increment_run_log(run_id, {"rows_failed": 1})
                    if consecutive_session_fail >= 3:
                        raise RuntimeError("HTTP session expired 3 times in a row; re-run to log in again.")
                except Exception as row_err:
                    failed_keys.append(key)
                    logger.error(f"FAILED {g}/{o} (NOT saved): {row_err}")
                    if use_db and run_id:
                        self.db.increment_run_log(run_id, {"rows_failed": 1})

            if page_num >= max_pages:
                logger.warning(f"Reached page safety limit ({max_pages}).")
                break
            if not self._go_next_page(info):
                break
            page_num += 1

        logger.info(f"Finished {target}: saved/collected={len(records)}, failed={len(failed_keys)}, "
                    f"unique rows seen={len(seen)}, site total={total_expected}.")
        if total_expected is not None and len(seen) != total_expected:
            logger.warning(f"COMPLETENESS MISMATCH: site says {total_expected} records but only {len(seen)} unique rows were "
                           f"seen across pages. The portal's pages overlap; some records were NOT reached.")
        if failed_keys:
            logger.warning(f"Failed records (re-run to retry): {failed_keys}")
        return records

    def close(self):
        try:
            if self.driver is not None:
                d = self.driver
                self.driver = None
                d.quit()
        except Exception:
            pass
