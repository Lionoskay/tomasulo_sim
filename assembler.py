"""
汇编器：将 MIPS 浮点指令文本解析为内部表示。
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Instruction:
    """单条指令的内部表示。"""
    addr: int          # 指令地址（从 0 开始，每指令 4 字节）
    op: str            # 操作码: l.d / s.d / add.d / sub.d / mul.d / div.d
    rd: Optional[str]  # 目标寄存器（FP 或整数）
    rs: Optional[str]  # 源寄存器 1（整数，用于 load/store 基址；FP 用于运算）
    rt: Optional[str]  # 源寄存器 2（FP 用于运算；load/store 中为 FP 数据寄存器）
    offset: int = 0    # 立即数偏移（仅 load/store）
    text: str = ""     # 原始文本

    @property
    def is_load(self) -> bool:
        return self.op == "l.d"

    @property
    def is_store(self) -> bool:
        return self.op == "s.d"

    @property
    def is_fp_arith(self) -> bool:
        return self.op in ("add.d", "sub.d", "mul.d", "div.d")


class ParseError(Exception):
    """汇编解析错误。"""
    def __init__(self, line_no: int, message: str):
        self.line_no = line_no
        self.message = message
        super().__init__(f"第{line_no}行: {message}")


# 仅支持偶数编号 FP 寄存器
VALID_FP_REGS = {f"$f{i}" for i in range(0, 32, 2)}
# 整数寄存器 R0-R31
VALID_INT_REGS = {f"R{i}" for i in range(32)}

# 指令正则
RE_LOAD_STORE = re.compile(
    r"^(l\.d|s\.d)\s+(\$f\d+)\s*,\s*(-?\d+)\s*\(\s*([rR]\d+)\s*\)\s*$"
)
RE_FP_ARITH = re.compile(
    r"^(add\.d|sub\.d|mul\.d|div\.d)\s+(\$f\d+)\s*,\s*(\$f\d+)\s*,\s*(\$f\d+)\s*$"
)


def parse_line(line: str, line_no: int, addr: int) -> Optional[Instruction]:
    """解析单行指令文本，返回 Instruction 或 None（空行/注释）。"""
    # 去除注释和首尾空白
    stripped = line.split("#")[0].strip()
    if not stripped:
        return None  # 空行

    # 尝试 load/store 格式
    m = RE_LOAD_STORE.match(stripped)
    if m:
        op, ft, offset, rs = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if ft not in VALID_FP_REGS:
            raise ParseError(line_no, f"无效的 FP 寄存器: {ft}")
        if rs.upper() not in VALID_INT_REGS:
            raise ParseError(line_no, f"无效的整数寄存器: {rs}")
        if op == "l.d":
            return Instruction(addr=addr, op=op, rd=ft, rs=rs, rt=None, offset=offset, text=stripped)
        else:  # s.d
            return Instruction(addr=addr, op=op, rd=None, rs=rs, rt=ft, offset=offset, text=stripped)

    # 尝试 FP 算术格式
    m = RE_FP_ARITH.match(stripped)
    if m:
        op, fd, fs, ft = m.group(1), m.group(2), m.group(3), m.group(4)
        for r in (fd, fs, ft):
            if r not in VALID_FP_REGS:
                raise ParseError(line_no, f"无效的 FP 寄存器: {r}")
        return Instruction(addr=addr, op=op, rd=fd, rs=fs, rt=ft, offset=0, text=stripped)

    raise ParseError(line_no, f"无法识别的指令格式: {stripped}")


def assemble(code: str) -> list[Instruction]:
    """将程序文本解析为指令列表，每指令 4 字节地址对齐。"""
    instructions = []
    lines = code.strip().split("\n")
    addr = 0
    for line_no, line in enumerate(lines, start=1):
        try:
            inst = parse_line(line, line_no, addr)
        except ParseError:
            raise
        if inst is not None:
            instructions.append(inst)
            addr += 4
    return instructions
