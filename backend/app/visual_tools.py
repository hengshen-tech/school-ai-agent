"""Scoped image inspection commands for one Agent; no OCR or MCP server."""
import argparse
import ast
import json
import math
import shlex
import sys
import uuid
from fractions import Fraction
from pathlib import Path

from PIL import Image


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def command_parser():
    parser = Parser()
    parser.add_argument('--manifest', required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    crop = actions.add_parser('crop')
    pages = crop.add_mutually_exclusive_group(required=True)
    pages.add_argument('--page', type=int)
    pages.add_argument('--reference', type=int)
    crop.add_argument('--bbox', type=float, nargs=4, required=True)
    crop.add_argument('--scale', type=float, default=2)
    calc = actions.add_parser('calc')
    calc.add_argument('--expression', required=True)
    return parser


def calculate(expression):
    if len(expression) > 200:
        raise ValueError('算式过长')
    tree = ast.parse(expression, mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError('算式过于复杂')
    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value) or abs(node.value) > 1e9:
                raise ValueError('数字超出计算范围')
            result = Fraction(str(node.value))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add): result = left + right
            elif isinstance(node.op, ast.Sub): result = left - right
            elif isinstance(node.op, ast.Mult): result = left * right
            elif isinstance(node.op, ast.Div): result = left / right
            elif isinstance(node.op, ast.Pow) and right.denominator == 1 and abs(right) <= 100:
                result = left ** int(right)
            else: raise ValueError('只支持加减乘除和小整数次幂')
        else:
            raise ValueError('仅允许算术表达式，不执行 Python 代码')
        if max(result.numerator.bit_length(), result.denominator.bit_length()) > 4096:
            raise ValueError('计算结果过大')
        return result
    return {'expression': expression, 'result': str(evaluate(tree.body))}


def crop_image(manifest, args):
    reference = args.reference is not None
    number = args.reference if reference else args.page
    source = manifest['references' if reference else 'pages']
    if number is None or not 1 <= number <= len(source):
        raise ValueError('页码超出本次任务范围')
    x1, y1, x2, y2 = args.bbox
    if not all(math.isfinite(v) and 0 <= v <= 1000 for v in args.bbox) or x2 <= x1 or y2 <= y1:
        raise ValueError('裁剪框必须是原图内 0–1000 坐标，右下角大于左上角')
    if not math.isfinite(args.scale) or not 1 <= args.scale <= 4:
        raise ValueError('放大倍数必须为 1–4')
    with Image.open(source[number - 1]) as original:
        width, height = original.size
        pixels = [round(x1 * width / 1000), round(y1 * height / 1000),
                  round(x2 * width / 1000), round(y2 * height / 1000)]
        if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
            raise ValueError('裁剪区域过小，请扩大范围')
        image = original.convert('RGB').crop(pixels)
    factor = min(args.scale, 2400 / max(image.size))
    image = image.resize((max(1, round(image.width * factor)), max(1, round(image.height * factor))), Image.Resampling.LANCZOS)
    directory = Path(manifest['views'])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{"reference" if reference else "page"}-{number}-{uuid.uuid4().hex[:10]}.png'
    image.save(path)
    return {'path': str(path), 'page': number, 'kind': 'reference' if reference else 'upload',
        'original_size': [width, height], 'requested_bbox': args.bbox,
        'original_bbox': [pixels[0]*1000/width, pixels[1]*1000/height, pixels[2]*1000/width, pixels[3]*1000/height],
        'original_pixels': pixels,
        'display_size': list(image.size),
        'mapping': '原图归一化 x=x1+局部归一化x*(x2-x1)/1000；y 同理。最终答案框必须返回上传原图坐标。'}


class VisualTools:
    def __init__(self, pages, references, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.views = self.directory / 'views'
        self.pages = [str(Path(p).resolve()) for p in pages]
        self.references = [str(Path(r['path']).resolve()) for r in references]
        self.allowed_images = set(self.pages + self.references)
        self.manifest = self.directory / 'images.json'
        self.manifest.write_text(json.dumps({'pages': self.pages, 'references': self.references,
                                             'views': str(self.views)}, ensure_ascii=False))
        self.prefix = [sys.executable, str(Path(__file__).resolve()), '--manifest', str(self.manifest)]
        self.command_prefix = shlex.join(self.prefix)

    async def authorize(self, data, tool_use_id, context):
        specific = {'hookEventName': 'PreToolUse'}
        try:
            name, args = data.get('tool_name'), dict(data.get('tool_input') or {})
            if name == 'Read':
                path = Path(args.get('file_path', ''))
                path = (path if path.is_absolute() else self.directory / path).resolve()
                if not path.is_file() or (str(path) not in self.allowed_images and not (
                    path.is_relative_to(self.views) and path.suffix.lower() == '.png')):
                    raise ValueError('只能读取本任务原图、参考图和裁图片')
                args['file_path'] = str(path)
            elif name == 'Bash':
                parts = shlex.split(args.get('command', ''))
                if parts[:len(self.prefix)] != self.prefix:
                    raise ValueError('仅允许使用输入提供的裁图/算术命令前缀')
                parsed = command_parser().parse_args(parts[2:])
                if parsed.action == 'calc': calculate(parsed.expression)
                # Canonical quoting prevents shell expansion, redirects and pipelines.
                args = {'command': shlex.join(parts), 'timeout': 30000, 'run_in_background': False}
            else:
                raise ValueError('本任务仅允许 Read 图像和 Bash 裁图/算术')
            specific.update(permissionDecision='allow', updatedInput=args)
        except (ValueError, TypeError, OSError, ZeroDivisionError, SyntaxError) as exc:
            specific.update(permissionDecision='deny', permissionDecisionReason=str(exc))
        return {'hookSpecificOutput': specific}


if __name__ == '__main__':
    try:
        args = command_parser().parse_args()
        manifest = json.loads(Path(args.manifest).read_text())
        result = calculate(args.expression) if args.action == 'calc' else crop_image(manifest, args)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, TypeError, OSError, KeyError, ZeroDivisionError, SyntaxError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(2)
