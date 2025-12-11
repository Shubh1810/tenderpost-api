"""CAPTCHA handling module for 2Captcha integration."""

from .solver import solve_captcha_2captcha, test_2captcha_connectivity
from .screenshot import capture_captcha_screenshot, preprocess_captcha_image

__all__ = [
    "solve_captcha_2captcha",
    "test_2captcha_connectivity",
    "capture_captcha_screenshot",
    "preprocess_captcha_image",
]

