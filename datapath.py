"""
数据通路：浮点寄存器堆、整数寄存器堆、数据内存、指令内存、功能部件。
"""

from dataclasses import dataclass, field
from typing import Optional


# ── 浮点寄存器堆（16 个双精度，仅偶数编号 F0-F30） ──

FP_REGISTERS = [f"f{i}" for i in range(0, 32, 2)]


@dataclass
class FPRegisterFile:
    """16 个 64 位双精度 FP 寄存器，仅偶数编号。"""
    regs: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        for name in FP_REGISTERS:
            self.regs.setdefault(name, 0.0)

    def read(self, name: str) -> float:
        return self.regs[name]

    def write(self, name: str, value: float):
        self.regs[name] = value


# ── 整数寄存器堆（32 个 32 位，$0 硬连线为 0） ──

INT_REGISTERS = [f"{i}" for i in range(32)]


@dataclass
class IntRegisterFile:
    """32 个 32 位整数寄存器，$0 硬连线为 0。"""
    regs: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        for name in INT_REGISTERS:
            self.regs.setdefault(name, 0)

    def read(self, name: str) -> int:
        return self.regs[name.lstrip("$Rr")]

    def write(self, name: str, value: int):
        name = name.lstrip("$Rr")
        if name == "0":
            return  # R0 硬连线为 0，不可写
        self.regs[name] = value


# ── 数据内存 ──

@dataclass
class DataMemory:
    """字节寻址数据内存，支持 l.d/s.d 双字存取。"""
    memory: dict[int, float] = field(default_factory=dict)

    def read_double(self, address: int) -> float:
        return self.memory.get(address, 0.0)

    def write_double(self, address: int, value: float):
        self.memory[address] = value

    def to_dict(self) -> dict[str, float]:
        """转为 JSON 可序列化的 dict（key 为字符串）。"""
        return {str(k): v for k, v in sorted(self.memory.items())}


# ── 指令内存 ──

@dataclass
class InstructionMemory:
    """按地址索引的指令内存。"""
    instructions: dict[int, str] = field(default_factory=dict)

    def load(self, addr: int) -> Optional[str]:
        return self.instructions.get(addr)

    def clear(self):
        self.instructions.clear()


# ── 功能部件延迟配置 ──

@dataclass
class LatencyConfig:
    load: int = 2
    store: int = 1
    add: int = 2
    sub: int = 2
    mul: int = 10
    div: int = 40

    def get(self, op: str) -> int:
        mapping = {
            "l.d": self.load,
            "s.d": self.store,
            "add.d": self.add,
            "sub.d": self.sub,
            "mul.d": self.mul,
            "div.d": self.div,
        }
        return mapping.get(op, 1)

    def to_dict(self) -> dict[str, int]:
        return {
            "load": self.load,
            "store": self.store,
            "add": self.add,
            "sub": self.sub,
            "mul": self.mul,
            "div": self.div,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LatencyConfig":
        return cls(
            load=d.get("load", 2),
            store=d.get("store", 1),
            add=d.get("add", 2),
            sub=d.get("sub", 2),
            mul=d.get("mul", 10),
            div=d.get("div", 40),
        )
