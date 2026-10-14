import unicodedata
import re

# Spec exact accent map + common Spanish diacritics
ACCENTS_MAP = str.maketrans({
    'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ö': 'o', 'ú': 'u', 'ü': 'u',
    'Á': 'A', 'É': 'E', 'Í': 'I', 'Ó': 'O', 'Ö': 'O', 'Ú': 'U', 'Ü': 'U',
    'ñ': 'N', 'Ñ': 'N',
})

def clean_text(text) -> str:
    """
    Transforms accents, cleans extra spaces/newlines, and converts to uppercase per TS Spec.
    Rule 0: Ensure valid UTF-8 string
    Rule 1: Transform 'áéíóöúüÁÉÍÓÖÚÜ' to 'aeioouuAEIOOUU'
    Rule 2: Strip multiple spaces, newlines, and tabs -> single space
    Rule 3: All in upper()
    """
    if text is None:
        return ""
    
    s = str(text).translate(ACCENTS_MAP)
    # Further normalize any remaining diacritical marks
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    # Collapse multiple whitespaces and newlines
    s = ' '.join(s.split())
    return s.upper().strip()

def clean_record(record):
    """Recursively clean all string values in a dictionary or list, except URLs and dates."""
    EXCLUDED_KEYS = {"link_garantia", "link_operacion", "source_html", "source_pdf", "fecha_insercion", "_id"}
    
    if isinstance(record, dict):
        cleaned = {}
        for k, v in record.items():
            if k in EXCLUDED_KEYS:
                cleaned[k] = v
            else:
                cleaned[k] = clean_record(v)
        return cleaned
    elif isinstance(record, list):
        return [clean_record(item) for item in record]
    elif isinstance(record, str):
        return clean_text(record)
    else:
        return record
