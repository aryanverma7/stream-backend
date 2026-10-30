"""Regenerates the credit-OCR fixture images: python tests/fixtures/make_credit_fixtures.py

Styled like the agent's calibrated crop: the "MIN NEXT ROUND" label and the value in light
bold type on Valorant's near-black buy-menu panel. Needs Pillow and the DejaVu fonts.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG, LABEL, VALUE = (14, 16, 20), (150, 156, 164), (236, 232, 225)


def crop(text, label=True):
    im = Image.new("RGB", (460, 40), BG)
    d = ImageDraw.Draw(im)
    if label:
        d.text((12, 9), "MIN NEXT ROUND", font=ImageFont.truetype(FONT, 18), fill=LABEL)
    # The value is set larger than the label, as in the game; at the label's size a lone 0 reads as "O".
    d.text((300 if label else 12, 9), text, font=ImageFont.truetype(FONT, 22), fill=VALUE)
    return im


for name, text, label in (("credits_4900.png", "4900", True), ("credits_8700.png", "8700", True),
                          ("credits_13100.png", "13100", True), ("credits_0.png", "0", True),
                          ("credits_5700_comma.png", "5,700", True),
                          ("credits_113_not_multiple_of_10.png", "113", True),
                          ("credits_no_label_120.png", "120", False)):
    crop(text, label).save(HERE / name)
# Closer to a real capture: slightly blurred and JPEG-compressed.
crop("4900").filter(ImageFilter.GaussianBlur(0.8)).save(HERE / "credits_noisy.jpg", quality=35)
