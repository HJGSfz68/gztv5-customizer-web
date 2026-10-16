#!/usr/bin/env python3
"""Customize the existing GZTV5 BlackACE APK without rebuilding its resource table."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from PIL import Image, ImageOps

PRIVATE_DIR = Path(__file__).resolve().parent / 'private'
DEFAULT_BASE = Path(os.environ.get('GZTV5_BASE_APK', str(PRIVATE_DIR / 'base.apk')))
KEYSTORE = Path(os.environ.get('GZTV5_KEYSTORE', str(PRIVATE_DIR / 'signing.p12')))
PASSFILE = Path(os.environ.get('GZTV5_PASSFILE', str(PRIVATE_DIR / 'signing.pass')))
ICON_ENTRY = 'res/wI.png'  # Obfuscated ZIP path for drawable/ic_launcher in this APK.
ANDROID_MANIFEST = 'AndroidManifest.xml'
MAX_ICON_BYTES = 20 * 1024 * 1024
PACKAGE_RE = re.compile(r'^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$')
DEX_RE = re.compile(r'^classes(?:(\d+))?\.dex$')


def run(args: list[object], *, capture: bool = False, log: Path | None = None) -> str:
    command = [str(x) for x in args]
    if log is not None:
        with log.open('w', encoding='utf-8') as f:
            p = subprocess.run(command, stdout=f, stderr=subprocess.STDOUT, text=True)
        if p.returncode:
            tail = log.read_text(errors='replace').splitlines()[-25:]
            raise RuntimeError(f"命令失败：{' '.join(command[:3])}\n" + '\n'.join(tail))
        return ''
    p = subprocess.run(command, check=True, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None, text=capture)
    return p.stdout if capture else ''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_signature_entry(name: str) -> bool:
    upper = name.upper()
    return upper == 'META-INF/MANIFEST.MF' or (
        upper.startswith('META-INF/') and upper.endswith(('.SF', '.RSA', '.DSA', '.EC'))
    )


def parse_badging(apk: Path) -> tuple[str, str, str]:
    text = run(['aapt', 'dump', 'badging', apk], capture=True)
    package = re.search(r"^package: name='([^']+)'", text, re.M)
    label = re.search(r"^application-label:'(.*)'$", text, re.M)
    version = re.search(r"^package: .*versionName='([^']*)'", text, re.M)
    if not package or not label:
        raise RuntimeError('无法从 APK 读取当前包名或显示名称。')
    return package.group(1), label.group(1), version.group(1) if version else ''


def read_len8(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    if first & 0x80:
        return (((first & 0x7F) << 8) | data[pos + 1]), pos + 2
    return first, pos + 1


def write_len8(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length > 0x7FFF:
        raise ValueError('Manifest 字符串过长。')
    return bytes([(length >> 8) | 0x80, length & 0xFF])


def read_len16(data: bytes, pos: int) -> tuple[int, int]:
    first = struct.unpack_from('<H', data, pos)[0]
    if first & 0x8000:
        second = struct.unpack_from('<H', data, pos + 2)[0]
        return (((first & 0x7FFF) << 16) | second), pos + 4
    return first, pos + 2


def write_len16(length: int) -> bytes:
    if length < 0x8000:
        return struct.pack('<H', length)
    if length > 0x7FFFFFFF:
        raise ValueError('Manifest 字符串过长。')
    return struct.pack('<HH', ((length >> 16) | 0x8000), length & 0xFFFF)


def decode_mutf8(data: bytes) -> str:
    """Decode Android's modified UTF-8 into a Python string."""
    units: list[int] = []
    i = 0
    while i < len(data):
        b0 = data[i]
        if b0 == 0:
            break
        if b0 < 0x80:
            units.append(b0)
            i += 1
        elif (b0 & 0xE0) == 0xC0:
            if i + 1 >= len(data):
                raise ValueError('不完整的 MUTF-8 双字节序列。')
            units.append(((b0 & 0x1F) << 6) | (data[i + 1] & 0x3F))
            i += 2
        elif (b0 & 0xF0) == 0xE0:
            if i + 2 >= len(data):
                raise ValueError('不完整的 MUTF-8 三字节序列。')
            units.append(((b0 & 0x0F) << 12) | ((data[i + 1] & 0x3F) << 6) | (data[i + 2] & 0x3F))
            i += 3
        else:
            raise ValueError('Manifest 中出现非 MUTF-8 字节序列。')
    raw = b''.join(struct.pack('<H', u) for u in units)
    return raw.decode('utf-16le', errors='surrogatepass')


def encode_mutf8(text: str) -> bytes:
    raw = text.encode('utf-16le', errors='surrogatepass')
    out = bytearray()
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        if unit == 0:
            out.extend((0xC0, 0x80))
        elif unit <= 0x7F:
            out.append(unit)
        elif unit <= 0x7FF:
            out.extend((0xC0 | (unit >> 6), 0x80 | (unit & 0x3F)))
        else:
            out.extend((0xE0 | (unit >> 12), 0x80 | ((unit >> 6) & 0x3F), 0x80 | (unit & 0x3F)))
    return bytes(out)


def align4(value: int) -> int:
    return (value + 3) & ~3


def patch_string_pool(chunk: bytes, old_package: str, new_package: str,
                      old_label: str, new_label: str) -> tuple[bytes, dict[str, int]]:
    if len(chunk) < 28:
        raise ValueError('Manifest string pool 不完整。')
    chunk_type, header_size, chunk_size = struct.unpack_from('<HHI', chunk, 0)
    if chunk_type != 0x0001 or header_size < 28 or chunk_size > len(chunk):
        raise ValueError('Manifest string pool 头部无效。')
    string_count, style_count, flags, strings_start, styles_start = struct.unpack_from('<IIIII', chunk, 8)
    if string_count > 100000 or style_count > 100000:
        raise ValueError('Manifest string pool 数量异常。')
    arrays_pos = header_size
    arrays_end = arrays_pos + 4 * (string_count + style_count)
    if arrays_end > chunk_size or strings_start > chunk_size:
        raise ValueError('Manifest string pool offsets 越界。')
    string_offsets = list(struct.unpack_from('<' + 'I' * string_count, chunk, arrays_pos)) if string_count else []
    style_offsets_pos = arrays_pos + 4 * string_count
    style_offsets = list(struct.unpack_from('<' + 'I' * style_count, chunk, style_offsets_pos)) if style_count else []
    utf8 = bool(flags & 0x100)
    strings: list[str] = []
    for offset in string_offsets:
        pos = strings_start + offset
        if pos >= chunk_size:
            raise ValueError('Manifest string offset 越界。')
        if utf8:
            _, pos = read_len8(chunk, pos)  # UTF-16 code-unit length
            byte_length, pos = read_len8(chunk, pos)
            end = pos + byte_length
            strings.append(decode_mutf8(chunk[pos:end]))
        else:
            char_length, pos = read_len16(chunk, pos)
            end = pos + char_length * 2
            strings.append(chunk[pos:end].decode('utf-16le', errors='surrogatepass'))

    counts = {'package_strings': 0, 'label_strings': 0}
    rewritten: list[str] = []
    for value in strings:
        if value == old_label:
            value = new_label
            counts['label_strings'] += 1
        if old_package and old_package in value:
            counts['package_strings'] += value.count(old_package)
            value = value.replace(old_package, new_package)
        rewritten.append(value)

    encoded_items: list[bytes] = []
    new_offsets: list[int] = []
    running = 0
    for value in rewritten:
        new_offsets.append(running)
        if utf8:
            body = encode_mutf8(value)
            utf16_units = len(value.encode('utf-16le', errors='surrogatepass')) // 2
            item = write_len8(utf16_units) + write_len8(len(body)) + body + b'\x00'
        else:
            body = value.encode('utf-16le', errors='surrogatepass')
            units = len(body) // 2
            item = write_len16(units) + body + b'\x00\x00'
        encoded_items.append(item)
        running += len(item)
    strings_blob = b''.join(encoded_items)

    new_strings_start = align4(header_size + 4 * (string_count + style_count))
    prefix = bytearray(chunk[:header_size])
    if string_count:
        prefix.extend(struct.pack('<' + 'I' * string_count, *new_offsets))
    if style_count:
        prefix.extend(struct.pack('<' + 'I' * style_count, *style_offsets))
    prefix.extend(b'\x00' * (new_strings_start - len(prefix)))
    prefix.extend(strings_blob)

    if style_count:
        if styles_start == 0 or styles_start > chunk_size:
            raise ValueError('Manifest style data offsets 无效。')
        new_styles_start = align4(len(prefix))
        prefix.extend(b'\x00' * (new_styles_start - len(prefix)))
        prefix.extend(chunk[styles_start:chunk_size])
    else:
        new_styles_start = 0
        prefix.extend(b'\x00' * (align4(len(prefix)) - len(prefix)))

    new_chunk = bytearray(prefix)
    struct.pack_into('<I', new_chunk, 4, len(new_chunk))
    struct.pack_into('<I', new_chunk, 16, flags & ~0x1)  # clear sorted-string flag after replacements
    struct.pack_into('<I', new_chunk, 20, new_strings_start)
    struct.pack_into('<I', new_chunk, 24, new_styles_start)
    return bytes(new_chunk), counts


def patch_binary_manifest(data: bytes, old_package: str, new_package: str,
                          old_label: str, new_label: str) -> tuple[bytes, dict[str, int]]:
    if len(data) < 8:
        raise ValueError('AndroidManifest.xml 为空或过短。')
    root_type, root_header, root_size = struct.unpack_from('<HHI', data, 0)
    if root_type != 0x0003 or root_header < 8 or root_size > len(data):
        raise ValueError('输入不是有效的 Android Binary XML Manifest。')
    pos = root_header
    while pos + 8 <= root_size:
        kind, header_size, chunk_size = struct.unpack_from('<HHI', data, pos)
        if chunk_size < 8 or pos + chunk_size > root_size:
            raise ValueError('Manifest chunk 长度异常。')
        if kind == 0x0001:
            pool, counts = patch_string_pool(data[pos:pos + chunk_size], old_package,
                                             new_package, old_label, new_label)
            if counts['package_strings'] < 1:
                raise ValueError('Manifest 字符串池中未找到原包名。')
            if counts['label_strings'] < 1:
                raise ValueError('Manifest 字符串池中未找到当前应用名称。')
            result = bytearray(data[:pos] + pool + data[pos + chunk_size:root_size] + data[root_size:])
            struct.pack_into('<I', result, 4, root_size + len(pool) - chunk_size)
            return bytes(result), counts
        pos += chunk_size
    raise ValueError('AndroidManifest.xml 中未找到 string pool。')


def smali_escape(value: str) -> str:
    out: list[str] = []
    for ch in value:
        cp = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif cp < 0x20 or cp > 0x7E:
            if cp <= 0xFFFF:
                out.append(f'\\u{cp:04x}')
            else:
                cp -= 0x10000
                out.append(f'\\u{0xD800 + (cp >> 10):04x}\\u{0xDC00 + (cp & 0x3FF):04x}')
        else:
            out.append(ch)
    return ''.join(out)


def validate_inputs(name: str, package: str, url: str) -> None:
    if not name or len(name) > 50 or any(ord(c) < 0x20 for c in name):
        raise ValueError('应用名称必填，最多 50 个字符，且不能包含控制字符。')
    if not PACKAGE_RE.fullmatch(package):
        raise ValueError('包名格式无效；请使用小写形式，例如 com.example.myapp。')
    if len(url) > 2048 or any(c.isspace() for c in url):
        raise ValueError('网址长度无效或包含空格。')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('网址必须是完整的 http:// 或 https:// 地址。')


def make_icon_png(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(f'找不到图标文件：{path}')
    if path.stat().st_size > MAX_ICON_BYTES:
        raise ValueError('图标文件不能超过 20 MB。')
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        has_alpha = ('A' in im.getbands()) or ('transparency' in im.info)
        im = ImageOps.fit(im, (512, 512), method=Image.Resampling.LANCZOS,
                          centering=(0.5, 0.5))
        im = im.convert('RGBA' if has_alpha else 'RGB')
        buf = io.BytesIO()
        im.save(buf, format='PNG', optimize=True)
        return buf.getvalue()


def signer_fingerprint(apk: Path) -> str:
    text = run(['apksigner', 'verify', '--print-certs', apk], capture=True)
    match = re.search(r'Signer #1 certificate SHA-256 digest: ([0-9a-f]+)', text, re.I)
    if not match:
        raise RuntimeError('无法读取 APK 签名证书指纹。')
    return match.group(1).lower()


def customize(*, base_apk: Path, output_apk: Path, display_name: str,
              package_name: str, web_url: str, icon_path: Path | None,
              progress_callback: Callable[[str], None] | None = None,
              keep_failed_work: bool = True) -> Path:
    validate_inputs(display_name, package_name, web_url)
    if not base_apk.is_file():
        raise FileNotFoundError(f'找不到基准 APK：{base_apk}')
    if not KEYSTORE.is_file() or not PASSFILE.is_file():
        raise FileNotFoundError('这台电脑上的签名密钥不可用；请勿换用其它签名文件。')
    if base_apk.resolve() == output_apk.resolve():
        raise ValueError('输出文件不能覆盖基准 APK。')
    output_apk.parent.mkdir(parents=True, exist_ok=True)

    old_package, old_label, version = parse_badging(base_apk)
    work = Path(tempfile.mkdtemp(prefix='gztv5-customizer-'))
    ok = False

    def report(message: str) -> None:
        print(message)
        if progress_callback is not None:
            progress_callback(message)

    try:
        report(f'基准包：{old_package}  |  版本：{version or "未知"}')
        report('阶段 1/5：解码 DEX，保留原 APK 的资源表。')
        decoded = work / 'decoded'
        run(['apktool', 'd', '-f', base_apk, '-o', decoded], log=work / 'decode.log')

        app_config = decoded / 'smali/app/blackace/lib/AppConfig.smali'
        if not app_config.is_file():
            raise RuntimeError('找不到 BlackACE 主配置类 AppConfig.smali。')
        text = app_config.read_text(encoding='utf-8')
        method_re = re.compile(r'(?ms)^\.method public getWebUrl\(\)Ljava/lang/String;\n.*?^\.end method')
        replacement = (
            '.method public getWebUrl()Ljava/lang/String;\n'
            '    .locals 1\n'
            f'    const-string v0, "{smali_escape(web_url)}"\n'
            '    return-object v0\n'
            '.end method'
        )
        text, n = method_re.subn(replacement, text, count=1)
        if n != 1:
            raise RuntimeError('主网址 getter 与预期不一致，已停止以避免破坏 APK。')
        app_config.write_text(text, encoding='utf-8')

        # Keep any hard-coded provider references consistent with the renamed manifest package.
        for smali_file in decoded.rglob('*.smali'):
            source = smali_file.read_text(encoding='utf-8', errors='strict')
            if old_package in source:
                smali_file.write_text(source.replace(old_package, package_name), encoding='utf-8')

        binary_manifest: bytes
        with zipfile.ZipFile(base_apk, 'r') as zin:
            if ANDROID_MANIFEST not in zin.namelist():
                raise RuntimeError('APK 中缺少 AndroidManifest.xml。')
            binary_manifest = zin.read(ANDROID_MANIFEST)
            if ICON_ENTRY not in zin.namelist():
                raise RuntimeError(f'未找到图标资源 {ICON_ENTRY}；此工具仅适用于当前基准 APK。')
            base_names = set(zin.namelist())
            dex_entries = sorted((n for n in base_names if DEX_RE.fullmatch(n)),
                                 key=lambda n: int(DEX_RE.fullmatch(n).group(1) or 1))

        manifest_bytes, counts = patch_binary_manifest(
            binary_manifest, old_package, package_name, old_label, display_name)
        if icon_path is not None:
            icon_bytes = make_icon_png(icon_path)
        else:
            icon_bytes = None

        smali_roots = [p for p in decoded.iterdir()
                       if p.is_dir() and (p.name == 'smali' or p.name.startswith('smali_classes'))]
        smali_map: dict[str, Path] = {}
        for root in smali_roots:
            if root.name == 'smali':
                dex_name = 'classes.dex'
            else:
                suffix = root.name[len('smali_classes'):]
                if not suffix.isdigit():
                    continue
                dex_name = f'classes{suffix}.dex'
            smali_map[dex_name] = root
        if set(smali_map) != set(dex_entries):
            raise RuntimeError(f'DEX/Smali 布局不匹配：APK={dex_entries}, 解码目录={sorted(smali_map)}')

        compiled: dict[str, Path] = {}
        for dex_index, (dex_name, smali_root) in enumerate(smali_map.items(), start=1):
            report(f'阶段 2/5：编译 {dex_name}（{dex_index}/{len(smali_map)}），只固定主网址，不改代理配置。')
            dex_out = work / dex_name
            run(['smali', 'assemble', '--api', '28', '-o', dex_out, smali_root], log=work / f'{dex_name}.log')
            compiled[dex_name] = dex_out

        report('阶段 3/5：替换二进制 Manifest 字符串与图标。')
        unsigned = work / 'unsigned.apk'
        replacements: dict[str, bytes] = {ANDROID_MANIFEST: manifest_bytes}
        replacements.update({name: path.read_bytes() for name, path in compiled.items()})
        if icon_bytes is not None:
            replacements[ICON_ENTRY] = icon_bytes

        with zipfile.ZipFile(base_apk, 'r') as zin, zipfile.ZipFile(unsigned, 'w', allowZip64=True) as zout:
            for info in zin.infolist():
                if is_signature_entry(info.filename):
                    continue
                data = replacements.get(info.filename)
                if data is None:
                    data = zin.read(info.filename)
                zout.writestr(info, data)

        report('阶段 4/5：对齐并用当前证书签名。')
        aligned = work / 'aligned.apk'
        run(['zipalign', '-f', '4', unsigned, aligned], log=work / 'zipalign.log')
        if output_apk.exists():
            output_apk.unlink()
        # PKCS#12 uses the store password for its key; reading the same file twice reaches EOF.
        run(['apksigner', 'sign', '--ks', KEYSTORE, '--ks-type', 'PKCS12',
             '--ks-pass', f'file:{PASSFILE}', '--out', output_apk, aligned],
            log=work / 'sign.log')

        report('阶段 5/5：验证包名、显示名、图标、签名和未改动资产。')
        run(['apksigner', 'verify', '--verbose', output_apk], log=work / 'verify-signature.log')
        run(['zipalign', '-c', '-v', '4', output_apk], log=work / 'verify-alignment.log')
        run(['unzip', '-t', output_apk], log=work / 'verify-zip.log')
        new_package, new_label, _ = parse_badging(output_apk)
        if new_package != package_name or new_label != display_name:
            raise RuntimeError(f'打包后元数据不匹配：{new_package!r}/{new_label!r}')
        xmltree = run(['aapt', 'dump', 'xmltree', output_apk, ANDROID_MANIFEST], capture=True)
        if f'{package_name}.fileprovider' not in xmltree:
            raise RuntimeError('FileProvider authority 未随包名更新。')
        expected_signer = os.environ.get('GZTV5_SIGNING_CERT_SHA256', '').strip().replace(':', '').lower()
        if not expected_signer:
            expected_signer = signer_fingerprint(base_apk)
        if signer_fingerprint(output_apk) != expected_signer:
            raise RuntimeError('新 APK 的证书与配置的签名证书不一致。')

        with zipfile.ZipFile(base_apk, 'r') as before, zipfile.ZipFile(output_apk, 'r') as after:
            before_names = {n for n in before.namelist() if not is_signature_entry(n)}
            after_names = {n for n in after.namelist() if not is_signature_entry(n)}
            if before_names != after_names:
                raise RuntimeError('APK ZIP 条目集合发生意外变化。')
            allowed = {ANDROID_MANIFEST, ICON_ENTRY, *dex_entries}
            for name in sorted(before_names - allowed):
                if sha256(before.read(name)) != sha256(after.read(name)):
                    raise RuntimeError(f'非预期资源发生变化：{name}')
            for required in ('assets/1', 'assets/config.json', 'assets/geoip.dat',
                             'assets/geosite.dat', 'assets/ads-nl.txt'):
                if required in before_names and before.read(required) != after.read(required):
                    raise RuntimeError(f'核心代理/广告资源未保持原样：{required}')
            if icon_bytes is not None:
                with Image.open(io.BytesIO(after.read(ICON_ENTRY))) as img:
                    if img.format != 'PNG' or img.size != (512, 512):
                        raise RuntimeError('输出图标校验失败。')

        print(f'Manifest 替换计数：包名/Authority {counts["package_strings"]} 项，显示名 {counts["label_strings"]} 项。')
        print('已保持不变：assets/1（现有去广告/隐藏底栏插件）、assets/config.json（含代理开关）、代理节点数据库及其他资源。')
        print(f'完成：{output_apk}')
        print(f'包名：{new_package}  |  名称：{new_label}  |  网址：{web_url}')
        print(f'证书 SHA-256：{signer_fingerprint(output_apk)}')
        print(f'文件 SHA-256：{sha256(output_apk.read_bytes())}')
        ok = True
        return output_apk
    finally:
        if ok or not keep_failed_work:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f'构建未完成，诊断文件保留在：{work}', file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description='自定义现有 GZTV5 APK 的名称、包名、图标和网址')
    parser.add_argument('--config', default=str(Path(__file__).with_name('customization.json')),
                        help='JSON 配置文件路径')
    parser.add_argument('--name', dest='display_name')
    parser.add_argument('--package', dest='package_name')
    parser.add_argument('--url', dest='web_url')
    parser.add_argument('--icon', dest='icon_file')
    parser.add_argument('--base')
    parser.add_argument('--out')
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f'找不到配置文件：{config_path}')
    config = json.loads(config_path.read_text(encoding='utf-8'))

    def resolve_config_path(value: str | None, default: Path | None = None) -> Path | None:
        if not value:
            return default
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()

    base = resolve_config_path(args.base or config.get('base_apk'), DEFAULT_BASE)
    output_value = args.out or config.get('output_apk')
    package = args.package_name or config.get('package_name', 'com.gztv5.i31272')
    name = args.display_name or config.get('display_name', '瓜子影视')
    url = args.web_url or config.get('web_url', 'https://gztv5.com/')
    icon_value = args.icon_file if args.icon_file is not None else config.get('icon_file', '')
    icon = resolve_config_path(icon_value)
    if output_value:
        output = resolve_config_path(output_value)
    else:
        output = config_path.parent / 'output' / f'{package}.apk'
    customize(base_apk=base, output_apk=output, display_name=name,
              package_name=package, web_url=url, icon_path=icon)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'错误：{exc}', file=sys.stderr)
        raise SystemExit(1)
