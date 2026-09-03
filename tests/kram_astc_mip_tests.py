#!/usr/bin/env python3

import binascii
import pathlib
import struct
import subprocess
import sys
import tempfile
import zlib


KTX_IDENTIFIER = b"\xabKTX 11\xbb\r\n\x1a\n"
ASTC_6X6 = 0x93B4


def png_chunk(chunk_type, payload):
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(
        ">I", binascii.crc32(chunk_type + payload) & 0xFFFFFFFF
    )


def write_png(path, width, height, color):
    row = bytes(color) * width
    pixels = b"".join(b"\0" + row for _ in range(height))
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(pixels))
    data += png_chunk(b"IEND", b"")
    path.write_bytes(data)


def write_split_png(path, width, height, top, bottom):
    rows = []
    for y in range(height):
        rows.append(b"\0" + bytes(top if y < height // 2 else bottom) * width)
    pixels = b"".join(rows)
    data = b"\x89PNG\r\n\x1a\n"
    data += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    data += png_chunk(b"IDAT", zlib.compress(pixels))
    data += png_chunk(b"IEND", b"")
    path.write_bytes(data)


def parse_ktx(path):
    data = path.read_bytes()
    if len(data) < 64 or data[:12] != KTX_IDENTIFIER:
        raise AssertionError(f"{path} is not KTX1")

    fields = struct.unpack_from("<13I", data, 12)
    if fields[0] != 0x04030201:
        raise AssertionError(f"{path} is not little-endian KTX1")

    internal_format = fields[4]
    width = fields[6]
    height = fields[7]
    mip_count = fields[11]
    offset = 64 + fields[12]
    levels = []
    for _ in range(mip_count):
        if offset + 4 > len(data):
            raise AssertionError(f"{path} has a truncated mip header")
        image_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        end = offset + image_size
        if end > len(data):
            raise AssertionError(f"{path} has a truncated mip payload")
        levels.append((width, height, data[offset:end]))
        offset = end + ((4 - image_size % 4) % 4)
        width = max(1, width // 2)
        height = max(1, height // 2)

    if offset != len(data):
        raise AssertionError(f"{path} has trailing bytes")
    return internal_format, levels


def run(kram, args, expect_success=True):
    result = subprocess.run([str(kram), *args], text=True, capture_output=True)
    if (result.returncode == 0) != expect_success:
        raise AssertionError(
            f"command returned {result.returncode}: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def assert_dominant(pixel, channel):
    if pixel[channel] < 150 or pixel[channel] - max(pixel[(channel + 1) % 3], pixel[(channel + 2) % 3]) < 80:
        raise AssertionError(f"expected channel {channel} to dominate, got {tuple(pixel[:4])}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: kram_astc_mip_tests.py <kram>")
    kram = pathlib.Path(sys.argv[1]).resolve()

    with tempfile.TemporaryDirectory(prefix="kram-astc-mips-") as temp:
        temp = pathlib.Path(temp)
        base = temp / "base.png"
        mip1 = temp / "mip1.png"
        mip2 = temp / "mip2.png"
        wrong = temp / "wrong.png"
        write_png(base, 17, 13, (255, 0, 0, 255))
        write_png(mip1, 8, 6, (0, 255, 0, 255))
        write_png(mip2, 4, 3, (0, 0, 255, 255))
        write_png(wrong, 7, 6, (255, 255, 255, 255))

        bc3 = None
        transcoded = None
        source_levels = None
        for source_format in ("bc1", "bc3", "bc7"):
            source = temp / f"source-{source_format}.dds"
            output = temp / f"transcoded-{source_format}.ktx"
            run(kram, ["encode", "-i", str(base), "-f", source_format, "-o", str(source)])
            run(kram, ["encode", "-i", str(source), "-f", "astc6x6", "-o", str(output)])
            internal_format, levels = parse_ktx(output)
            if internal_format != ASTC_6X6:
                raise AssertionError(f"{source_format} transcode produced format 0x{internal_format:x}")
            if source_format == "bc3":
                bc3 = source
                transcoded = output
                source_levels = levels

        passthrough = temp / "passthrough.ktx"
        run(kram, ["encode", "-i", str(transcoded), "-f", "astc6x6", "-o", str(passthrough)])
        passthrough_format, passthrough_levels = parse_ktx(passthrough)
        if passthrough_format != ASTC_6X6 or source_levels != passthrough_levels:
            raise AssertionError("same-format ASTC pass-through changed mip payloads")

        ktx2 = temp / "same-format.ktx2"
        run(kram, ["encode", "-i", str(bc3), "-f", "bc3", "-o", str(ktx2)])
        if ktx2.read_bytes()[:12] != b"\xabKTX 20\xbb\r\n\x1a\n":
            raise AssertionError("same-format KTX2 output was not written")
        ktx2_transcode = temp / "ktx2-transcode.ktx"
        run(kram, ["encode", "-i", str(ktx2), "-f", "astc6x6", "-o", str(ktx2_transcode)])
        if parse_ktx(ktx2_transcode)[0] != ASTC_6X6:
            raise AssertionError("KTX2 compressed source did not transcode to ASTC 6x6")

        orientation_png = temp / "orientation.png"
        flipped = temp / "flipped.ktx"
        direct = temp / "direct.ktx"
        flipped_decoded = temp / "flipped-decoded.ktx"
        direct_decoded = temp / "direct-decoded.ktx"
        write_split_png(orientation_png, 18, 18, (255, 0, 0, 255), (0, 0, 255, 255))
        run(kram, ["encode", "-i", str(orientation_png), "-f", "astc6x6", "-flip", "-mipnone", "-o", str(flipped)])
        run(kram, ["encode", "-i", str(flipped), "-f", "astc6x6", "-mipnone", "-o", str(direct)])
        run(kram, ["decode", "-i", str(flipped), "-o", str(flipped_decoded)])
        run(kram, ["decode", "-i", str(direct), "-o", str(direct_decoded)])
        _, flipped_levels = parse_ktx(flipped_decoded)
        _, direct_levels = parse_ktx(direct_decoded)
        assert_dominant(flipped_levels[0][2], 2)
        assert_dominant(direct_levels[0][2], 0)

        explicit = temp / "explicit.ktx"
        run(
            kram,
            [
                "encode", "-i", str(base),
                "-mip", "1", str(mip1),
                "-mip", "2", str(mip2),
                "-f", "astc6x6", "-o", str(explicit),
            ],
        )
        explicit_format, explicit_levels = parse_ktx(explicit)
        if explicit_format != ASTC_6X6 or [(w, h) for w, h, _ in explicit_levels] != [(17, 13), (8, 6), (4, 3)]:
            raise AssertionError("explicit chain did not preserve its three supplied levels")

        capped = temp / "capped.ktx"
        run(kram, ["encode", "-i", str(bc3), "-f", "astc6x6", "-mipcount", "2", "-o", str(capped)])
        capped_format, capped_levels = parse_ktx(capped)
        if capped_format != ASTC_6X6 or [(w, h) for w, h, _ in capped_levels] != [(17, 13), (8, 6)]:
            raise AssertionError(f"-mipcount 2 produced {[(w, h) for w, h, _ in capped_levels]}")

        decoded = temp / "decoded.ktx"
        run(kram, ["decode", "-i", str(explicit), "-o", str(decoded)])
        _, decoded_levels = parse_ktx(decoded)
        assert_dominant(decoded_levels[0][2], 0)
        assert_dominant(decoded_levels[1][2], 1)
        assert_dominant(decoded_levels[2][2], 2)

        for name, args in (
            ("hole", ["-mip", "2", str(mip2)]),
            ("duplicate", ["-mip", "1", str(mip1), "-mip", "1", str(mip1)]),
            ("wrong-size", ["-mip", "1", str(wrong)]),
        ):
            output = temp / f"{name}.ktx"
            run(kram, ["encode", "-i", str(base), *args, "-f", "astc6x6", "-o", str(output)], False)
            if output.exists():
                raise AssertionError(f"failed {name} encode left an output file")

        embedded_output = temp / "embedded-mips.ktx"
        run(
            kram,
            ["encode", "-i", str(transcoded), "-mip", "1", str(mip1),
             "-f", "astc6x6", "-o", str(embedded_output)],
        )
        # A mip-chained base alongside explicit levels is dumper-style output:
        # the explicit chain is authoritative and only level 0 of the base is
        # used. The result must carry exactly the supplied levels.
        embedded_format, embedded_levels = parse_ktx(embedded_output)
        if embedded_format != ASTC_6X6 or [(w, h) for w, h, _ in embedded_levels] != [
                (17, 13), (8, 6)]:
            raise AssertionError(
                f"explicit encode over an embedded-mip base produced {[(w, h) for w, h, _ in embedded_levels]}")

        for name, args in (
            ("float-destination", ["-f", "rgba16f"]),
            ("optopaque", ["-f", "bc3", "-optopaque"]),
        ):
            output = temp / f"{name}.ktx"
            run(
                kram,
                ["encode", "-i", str(base), "-mip", "1", str(mip1), *args, "-o", str(output)],
                False,
            )
            if output.exists():
                raise AssertionError(f"explicit encode accepted {name}")

        truncated = temp / "truncated.dds"
        truncated.write_bytes(bc3.read_bytes()[:80])
        output = temp / "truncated.ktx"
        run(kram, ["encode", "-i", str(truncated), "-f", "astc6x6", "-o", str(output)], False)
        if output.exists():
            raise AssertionError("truncated transcode left an output file")


def write_bgra_dds(path, w, h, rgba):
    """Uncompressed 32-bit DDS with the ARGB8888 mask set (pixels stored B,G,R,A)."""
    import struct

    header = struct.pack(
        "<4s7I11I2I4s5I5I",
        b"DDS ", 124,
        0x41 | 0x1000,  # caps | height/width
        h, w,
        0, 0, 1,
        *(0,) * 11,  # reserved1
        32, 0x41,  # pixelformat size, DDSPF_ALPHAPIXELS | DDSPF_RGB
        b"\0\0\0\0", 32,
        0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000,  # R,G,B,A masks (BGRA byte order)
        0x1000, 0, 0, 0, 0,  # caps, caps2-4, reserved2
    )
    r, g, b, a = rgba
    pixels = bytes((b, g, r, a)) * (w * h)
    path.write_bytes(header + pixels)


def test_bgra_dds_source(kram, temp):
    """Red-dominant BGRA-byte-order DDS must encode and decode red-dominant."""
    base = temp / "bgra.dds"
    write_bgra_dds(base, 16, 16, (200, 30, 20, 255))
    encoded = temp / "bgra.ktx"
    run(kram, ["encode", "-i", str(base), "-f", "astc6x6", "-mipnone", "-o", str(encoded)])
    decoded = temp / "bgra-decoded.ktx"
    run(kram, ["decode", "-i", str(encoded), "-o", str(decoded)])

    data = decoded.read_bytes()
    offset = 64 + int.from_bytes(data[60:64], "little")
    size = int.from_bytes(data[offset:offset + 4], "little")
    px = data[offset + 4:offset + 4 + size]
    count = size // 4
    mean_r = sum(px[i * 4 + 0] for i in range(count)) / count
    mean_b = sum(px[i * 4 + 2] for i in range(count)) / count
    if mean_r < 150 or mean_b > 60:
        raise AssertionError(f"BGRA-byte-order DDS decode is swapped: R={mean_r:.0f} B={mean_b:.0f}")


if __name__ == "__main__":
    import contextlib

    with tempfile.TemporaryDirectory(prefix="kram-astc-mips-") as _temp:
        _kram = pathlib.Path(sys.argv[1]).resolve()
        test_bgra_dds_source(_kram, pathlib.Path(_temp))
    main()
