import os
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def get_final_url_with_selenium(url, timeout=10, binary_location=""):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--remote-debugging-port=9222")

    # default is local dev path
    if binary_location:
        chrome_options.binary_location = binary_location
    elif os.path.exists("/snap/bin/chromium"):
        chrome_options.binary_location = "/snap/bin/chromium"  # Ubuntu 20, 22 etc
    elif os.path.exists("/usr/lib/chromium/chromium"):
        chrome_options.binary_location = (
            "/usr/lib/chromium/chromium"  # Mint dep package
        )
    elif os.path.exists("/usr/lib/chromium-browser/chromium-browser"):
        chrome_options.binary_location = "/usr/lib/chromium-browser/chromium-browser"
    else:
        print("Please provide the chromium binary location")
        return url

    # service = Service("/usr/lib/chromium-browser/chromedriver")

    print(f"The chromium binary location: {chrome_options.binary_location}")
    driver = webdriver.Chrome(options=chrome_options)
    time.sleep(1)

    try:
        driver.get(url)
        initial_url = driver.current_url
        print(f"Initial URL: {initial_url}")

        # sleep 10 sec or so
        WebDriverWait(driver, timeout).until(
            lambda driver: driver.current_url != initial_url or True
        )

        time.sleep(1)

        final_url = driver.current_url
        print(f"Final URL: {final_url}")

        if final_url != initial_url:
            print("Redirection detected.")
        else:
            print("No redirection detected.")

        return final_url

    except Exception as e:
        print(f"Error while checking redirection: {e}")
        return url

    finally:
        driver.quit()


# example
if __name__ == "__main__":
    test_url = "http://example.com"
    result = get_final_url_with_selenium(test_url)
    if result:
        print(f"Result: {result}")
