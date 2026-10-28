TARGET_BYTES = 200 * 1024
TARGET_LABEL = "200 KB"

# Quality is deliberately traded away for size.  The first profile that gets
# below the hard limit wins, so the best possible quality is kept.
SMART_DPIS = (96, 72, 54, 40, 28, 18)
RASTER_PROFILES = (
    (110, 58),
    (90, 50),
    (72, 44),
    (58, 38),
    (46, 32),
    (36, 26),
    (28, 21),
    (22, 17),
    (17, 13),
    (13, 10),
    (10, 8),
    (7, 6),
    (5, 4),
    (3, 3),
    (2, 2),
    (1, 1),
)
CONTACT_PROFILES = (
    (54, 55),
    (42, 45),
    (32, 36),
    (24, 28),
    (18, 22),
    (13, 17),
    (9, 13),
    (6, 9),
    (4, 6),
    (2, 3),
    (1, 1),
)

# Per-page image output.  For JPG the tuple is (DPI, JPEG quality); for PNG
# only the DPI matters because png256 is used (palette PNG compresses well).
IMAGE_JPG_PROFILES = (
    (160, 62),
    (140, 56),
    (120, 50),
    (100, 44),
    (84, 38),
    (70, 33),
    (58, 28),
    (48, 23),
    (40, 19),
    (32, 15),
    (26, 12),
    (20, 10),
    (16, 8),
    (12, 6),
    (9, 5),
    (7, 4),
    (5, 3),
    (3, 2),
    (2, 1),
    (1, 1),
)
IMAGE_PNG_PROFILES = (
    (160, 0),
    (140, 0),
    (120, 0),
    (100, 0),
    (84, 0),
    (70, 0),
    (58, 0),
    (48, 0),
    (40, 0),
    (32, 0),
    (26, 0),
    (20, 0),
    (16, 0),
    (12, 0),
    (9, 0),
    (7, 0),
    (5, 0),
    (3, 0),
    (2, 0),
    (1, 0),
)
