"""Simple browser helper for Selenium automation (restored)."""

import os
import tempfile
import uuid
import logging
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/tmp/screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def create_driver(headless: bool = True) -> webdriver.Chrome:
    """Create a Chrome driver with basic stability options."""
    opts = Options()

    # Headless mode (default True for production)
    if headless:
        opts.add_argument("--headless=new")

    # Basic stability options
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-features=ThirdPartyCookieBlocking")

    # Disable password manager popups & allow third-party cookies (required for Microsoft OAuth)
    opts.add_experimental_option(
        "prefs",
        {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.password_manager_leak_detection": False,
            "profile.cookie_controls_mode": 0,
            "profile.block_third_party_cookies": False,
        },
    )

    # Avoid detection
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Enable performance logging (for network token extraction)
    caps = DesiredCapabilities.CHROME
    caps["goog:loggingPrefs"] = {"performance": "ALL"}
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    # Unique profile for isolation (lightweight, no BrowserWorker)
    user_data_dir = os.path.join(tempfile.gettempdir(), f"chrome_profile_{uuid.uuid4().hex[:8]}")
    opts.add_argument(f"--user-data-dir={user_data_dir}")

    return webdriver.Chrome(options=opts)


def cleanup_driver(driver: webdriver.Chrome) -> None:
    """Close the driver safely and clean up its temp profile."""
    import subprocess
    import platform
    import time
    import shutil

    # Extract the user-data-dir from this specific driver before quitting
    profile_dir = None
    try:
        for arg in driver.options.arguments:
            if '--user-data-dir=' in arg:
                profile_dir = arg.split('=', 1)[1]
                break
    except Exception:
        pass

    # First try graceful quit
    try:
        driver.quit()
    except Exception as e:
        logger.warning("Driver quit failed: %s", e)

    time.sleep(1)

    if platform.system() == "Windows":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe"], capture_output=True, timeout=10)
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["wmic", "process", "where", "name='chrome.exe'", "get", "commandline,processid"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.split('\n'):
                if 'chrome_profile_' in line and 'user-data-dir' in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            subprocess.run(["taskkill", "/F", "/PID", parts[-1]], capture_output=True, timeout=5)
                        except Exception:
                            pass
        except Exception:
            pass
    else:
        # Linux — don't pkill (might kill other workers' browsers)
        # Just clean up the profile directory
        pass

    # Clean up this driver's temp profile directory to free disk/memory
    if profile_dir:
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass


def kill_all_browsers() -> None:
    """
    Nuclear option: Force kill ALL Chrome and ChromeDriver processes.
    Call between pipeline steps and between processing chunks.
    """
    import subprocess
    import platform
    import time
    import glob
    import shutil

    logger.info("BROWSER CLEANUP: Killing all Chrome/ChromeDriver processes...")

    if platform.system() == "Windows":
        for proc_name in ["chromedriver.exe", "chrome.exe"]:
            try:
                subprocess.run(["taskkill", "/F", "/IM", proc_name], capture_output=True, timeout=10)
            except Exception:
                pass
    else:
        # Linux — kill everything Chrome-related
        for pattern in ["chromedriver", "chrome"]:
            try:
                subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True, timeout=5)
            except Exception:
                pass

    # Clean up ALL temp Chrome profile directories
    try:
        cleaned = 0
        for d in glob.glob("/tmp/chrome_profile_*"):
            try:
                shutil.rmtree(d, ignore_errors=True)
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            logger.info(f"BROWSER CLEANUP: Removed {cleaned} temp Chrome profiles")
    except Exception:
        pass

    # Wait for OS to reclaim memory
    time.sleep(3)
    logger.info("BROWSER CLEANUP: Done")


def take_screenshot(driver: webdriver.Chrome, name: str) -> str:
    """Take screenshot and return path."""
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{name}.png"
    path = os.path.join(SCREENSHOT_DIR, filename)

    try:
        driver.save_screenshot(path)
        return path
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return ""