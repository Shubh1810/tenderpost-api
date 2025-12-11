"""CAPTCHA screenshot capture and preprocessing utilities."""

import base64
from io import BytesIO
from typing import Optional

from PIL import Image, ImageEnhance
from playwright.async_api import Page


# CAPTCHA image selectors (priority order)
CAPTCHA_SELECTORS = [
    'img[src*="captcha"]',
    'img[alt*="captcha" i]',
    'img[id*="captcha" i]',
    '#captchaImage',
    'img[title*="captcha" i]',
]


def log_step(step: str, status: str, details: dict) -> None:
    """Print detailed logs for each pipeline step."""
    status_emoji = {
        "success": "✅",
        "error": "❌",
        "warning": "⚠️",
        "initiated": "🔵",
    }.get(status, "📌")

    print(f"{status_emoji} [{step}] {status.upper()}")
    for key, value in details.items():
        print(f"   • {key}: {value}")


async def capture_captcha_screenshot(page: Page, enhance: bool = True) -> Optional[str]:
    """
    Capture CAPTCHA image from the page and return as base64 string.

    Args:
        page: Playwright Page object
        enhance: Whether to apply image preprocessing (contrast, sharpness)

    Returns:
        Base64-encoded image string (without data URI prefix) or None if not found
    """
    log_step("CAPTCHA Screenshot", "initiated", {"action": "Detecting CAPTCHA element"})

    # Try each selector in priority order
    captcha_element = None
    used_selector = None

    for selector in CAPTCHA_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                # Verify element is visible
                is_visible = await element.is_visible()
                if is_visible:
                    captcha_element = element
                    used_selector = selector
                    break
        except Exception as e:
            continue

    if not captcha_element:
        log_step(
            "CAPTCHA Screenshot",
            "error",
            {"error": "CAPTCHA element not found", "selectors_tried": len(CAPTCHA_SELECTORS)},
        )
        return None

    log_step(
        "CAPTCHA Screenshot",
        "success",
        {"selector": used_selector, "action": "Capturing screenshot"},
    )

    try:
        # Capture screenshot as PNG bytes
        screenshot_bytes = await captcha_element.screenshot(type="png")

        # Optionally enhance the image
        if enhance:
            screenshot_bytes = preprocess_captcha_image(screenshot_bytes)

        # Convert to base64
        image_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")

        log_step(
            "CAPTCHA Screenshot",
            "success",
            {
                "size": f"{len(screenshot_bytes)} bytes",
                "base64_length": len(image_base64),
                "enhanced": enhance,
            },
        )

        return image_base64

    except Exception as e:
        log_step("CAPTCHA Screenshot", "error", {"error": str(e)})
        return None


def preprocess_captcha_image(image_bytes: bytes) -> bytes:
    """
    Preprocess CAPTCHA image to improve OCR accuracy.

    Enhancements:
    - Increase contrast
    - Sharpen image
    - Convert to grayscale (optional)

    Args:
        image_bytes: Raw image bytes

    Returns:
        Processed image bytes
    """
    try:
        # Open image
        image = Image.open(BytesIO(image_bytes))

        # Convert to RGB if necessary
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")

        # Enhance contrast
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.5)  # 1.5x contrast

        # Sharpen image
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)  # 2x sharpness

        # Optional: Convert to grayscale for better OCR
        # image = image.convert("L")

        # Save to bytes
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    except Exception as e:
        # If preprocessing fails, return original
        log_step("Image Preprocessing", "warning", {"error": str(e), "fallback": "Original image"})
        return image_bytes


async def detect_captcha_presence(page: Page) -> bool:
    """
    Check if CAPTCHA is present on the page.

    Args:
        page: Playwright Page object

    Returns:
        True if CAPTCHA detected, False otherwise
    """
    for selector in CAPTCHA_SELECTORS:
        try:
            element = await page.query_selector(selector)
            if element:
                is_visible = await element.is_visible()
                if is_visible:
                    return True
        except Exception:
            continue

    return False

