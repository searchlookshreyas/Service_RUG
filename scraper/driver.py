import os
import undetected_chromedriver as uc

def get_driver():
    options = uc.ChromeOptions()
    options.page_load_strategy = 'none'
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    
    prefs = {
        "download.default_directory": os.path.abspath(os.getcwd()),
        "download.prompt_for_download": False,
        "plugins.always_open_pdf_externally": True
    }
    options.add_experimental_option("prefs", prefs)
    
    # uc.Chrome automatically patches and manages the driver
    driver = uc.Chrome(options=options)
    return driver
