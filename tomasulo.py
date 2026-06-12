"""
Tomasulo 动态调度核心：三阶段（Issue / Execute / Write Result）+ CDB。
包含保留站、Load/Store Buffer、寄存器状态表、指令状态表、冲突处理。
"""

from dataclasses import dataclass, field
from typing import Optional
from assembler import Instruction, assemble
from datapath import (
    FPRegisterFile, IntRegisterFile, DataMemory, InstructionMemory,
    LatencyConfig, FP_REGISTERS,
)


# ── 保留站条目 ──

@dataclass
class ReservationEntry:
    name: str
    busy: bool = False
    op: Optional[str] = None
    vj: Optional[float] = None
    vk: Optional[float] = None
    qj: Optional[str] = None
    qk: Optional[str] = None
    remaining: int = 0
    dest: Optional[str] = None
    inst_addr: Optional[int] = None
    vj_label: Optional[str] = None  # 来自 Load 的 Mn 标签
    vk_label: Optional[str] = None
    exec_done: bool = False     # 执行已完成，等待进入写结果阶段
    write_ready: bool = False   # 已可写结果（执行完成后推一周期）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "busy": self.busy,
            "op": self.op,
            "vj": self.vj_label if self.vj_label else self.vj,
            "vk": self.vk_label if self.vk_label else self.vk,
            "qj": self.qj,
            "qk": self.qk,
            "remaining": self.remaining,
            "dest": self.dest,
        }

    def clear(self):
        self.busy = False
        self.op = None
        self.vj = None
        self.vk = None
        self.qj = None
        self.qk = None
        self.remaining = 0
        self.dest = None
        self.inst_addr = None
        self.vj_label = None
        self.vk_label = None
        self.exec_done = False
        self.write_ready = False


# ── Load/Store Buffer 条目 ──

@dataclass
class LoadStoreEntry:
    name: str
    busy: bool = False
    op: Optional[str] = None       # "l.d" / "s.d"
    address: Optional[int] = None  # 有效地址（数值）
    value: Optional[float] = None  # Load 的加载值 / Store 的待存值
    remaining: int = 0
    dest: Optional[str] = None     # Load 的目标 FP 寄存器
    # 内部字段（前端不暴露 Qj/Qk）
    qj: Optional[str] = None       # 等待基址寄存器
    qk: Optional[str] = None       # Store 等待数据寄存器
    vj: Optional[int] = None       # 基址寄存器就绪值
    vk: Optional[float] = None     # Store 数据就绪值
    inst_addr: Optional[int] = None
    # Store 专用：有效地址是否已计算
    address_ready: bool = False
    data_ready: bool = False
    exec_done: bool = False    # 执行已完成，等待进入写结果阶段
    write_ready: bool = False  # 已可写结果（执行完成后推一周期）
    # 符号显示用
    base_reg: Optional[str] = None  # 基址寄存器名（如 "R1"）
    store_src: Optional[str] = None # Store 的源 FP 寄存器（如 "$f4"）
    offset: int = 0                 # 偏移量

    def to_dict(self) -> dict:
        # 地址符号形式：R[Rn]+m
        addr_str = None
        if self.base_reg is not None:
            addr_str = f"R[{self.base_reg.lstrip('Rr')}]+{self.offset}"
        # 值符号形式
        val_str = None
        if self.op == "l.d":
            # Load：执行完成后（remaining=0）才从内存取得值，之前为 —
            if self.remaining == 0 and self.base_reg is not None:
                val_str = f"M[R[{self.base_reg.lstrip('Rr')}]+{self.offset}]"
        elif self.op == "s.d":
            if self.data_ready and self.vk is not None:
                val_str = self.vk
            elif self.store_src:
                val_str = self.store_src

        return {
            "name": self.name,
            "busy": self.busy,
            "op": self.op,
            "address": addr_str,
            "value": val_str,
            "remaining": self.remaining,
            "dest": self.dest,
        }

    def clear(self):
        self.busy = False
        self.op = None
        self.address = None
        self.value = None
        self.remaining = 0
        self.dest = None
        self.qj = None
        self.qk = None
        self.vj = None
        self.vk = None
        self.inst_addr = None
        self.address_ready = False
        self.data_ready = False
        self.exec_done = False
        self.write_ready = False
        self.base_reg = None
        self.store_src = None
        self.offset = 0


# ── 寄存器状态条目 ──

@dataclass
class RegisterStatusEntry:
    qi: Optional[str] = None   # 生产者 tag，None 表示值已就绪
    value: Optional[float] = 0.0
    value_label: Optional[str] = None  # 值的符号标签（如 M1, R[f0]）
    computation: Optional[str] = None  # 计算表达式（如 "M1 + M2"）

    def to_dict(self) -> dict:
        return {
            "qi": self.qi,
            "value": self.value_label if (self.value_label and self.qi is None) else (self.value if self.qi is None else None),
            "computation": self.computation if self.qi is None else None,
        }


# ── 指令状态条目 ──

@dataclass
class InstructionStatusEntry:
    addr: int
    text: str
    issue: Optional[int] = None
    execute_start: Optional[int] = None
    execute_end: Optional[int] = None
    write_result: Optional[int] = None
    done: bool = False

    def to_dict(self) -> dict:
        return {
            "addr": self.addr,
            "text": self.text,
            "issue": self.issue,
            "execute_start": self.execute_start,
            "execute_end": self.execute_end,
            "write_result": self.write_result,
            "done": self.done,
        }


# ── 事件日志条目 ──

@dataclass
class Event:
    cycle: int
    type: str  # "issue" / "execute" / "cdb" / "stall"
    message: str


# ── Tomasulo 引擎 ──

class TomasuloEngine:
    """Tomasulo 动态调度模拟器核心引擎。"""

    def __init__(self):
        # 硬件组件
        self.fp_regs = FPRegisterFile()
        self.int_regs = IntRegisterFile()
        self.data_mem = DataMemory()
        self.instr_mem = InstructionMemory()
        self.latency = LatencyConfig()

        # 保留站: FP Add x3, FP Mul x2
        self.add_rs = [
            ReservationEntry("Add1"), ReservationEntry("Add2"), ReservationEntry("Add3")
        ]
        self.mul_rs = [
            ReservationEntry("Mult1"), ReservationEntry("Mult2")
        ]

        # Load/Store Buffer: Load x3, Store x3
        self.load_buffers = [
            LoadStoreEntry("Load1"), LoadStoreEntry("Load2"), LoadStoreEntry("Load3")
        ]
        self.store_buffers = [
            LoadStoreEntry("Store1"), LoadStoreEntry("Store2"), LoadStoreEntry("Store3")
        ]

        # 寄存器结果状态: 16 个 FP 寄存器
        self.register_status: dict[str, RegisterStatusEntry] = {
            name: RegisterStatusEntry() for name in FP_REGISTERS
        }

        # 指令状态表
        self.instruction_status: list[InstructionStatusEntry] = []

        # 指令列表（汇编后）
        self.instructions: list[Instruction] = []

        # 模拟状态
        self.cycle: int = 0
        self.pc: int = 0          # 指向下一条待流出的指令（指令索引）
        self.running: bool = False
        self.done: bool = False

        # 历史快照
        self.history: list[dict] = []

        # 事件日志
        self.events: list[Event] = []

        # 值标签追踪（Mn），Load 和 ALU 指令共用
        self._value_counter: int = 0
        self._value_labels: dict[str, str] = {}  # tag → "Mn"

        # CDB 延迟队列：本周期写结果的生产者，下一周期才广播
        self._pending_cdb: list = []

        # 统计
        self.stats = {
            "raw_stalls": 0,
            "war_stalls": 0,       # Tomasulo 下 WAR 通常为 0
            "structural_stalls": 0,
        }

    # ── 加载程序 ──

    def load_program(self, code: str, latencies: Optional[dict] = None):
        """汇编并加载程序，重置所有状态。"""
        self.instructions = assemble(code)
        if latencies:
            self.latency = LatencyConfig.from_dict(latencies)

        self.reset()

        # 初始化数据内存（模拟预先存在的数据）
        self.data_mem.write_double(100, 3.14)   # MEM[$1+0]
        self.data_mem.write_double(108, 2.71)   # MEM[$1+8]
        self.data_mem.write_double(116, 0.0)    # MEM[$1+16]

        # 初始化整数寄存器
        self.int_regs.write("R1", 100)  # R1 = 基址 100

        # 构建指令状态表
        self.instruction_status = [
            InstructionStatusEntry(addr=inst.addr, text=inst.text)
            for inst in self.instructions
        ]

        # 将指令写入指令内存
        self.instr_mem.clear()
        for inst in self.instructions:
            self.instr_mem.instructions[inst.addr] = inst.text

    def reset(self):
        """重置模拟器。"""
        self.fp_regs = FPRegisterFile()
        self.int_regs = IntRegisterFile()
        self.data_mem = DataMemory()
        self.instr_mem = InstructionMemory()

        for rs in self.add_rs:
            rs.clear()
        for rs in self.mul_rs:
            rs.clear()
        for buf in self.load_buffers:
            buf.clear()
        for buf in self.store_buffers:
            buf.clear()

        self.register_status = {
            name: RegisterStatusEntry(value_label=f"R[{name}]") for name in FP_REGISTERS
        }
        self.instruction_status = []
        self.cycle = 0
        self.pc = 0
        self.running = False
        self.done = False
        self.history = []
        self.events = []
        self._value_counter = 0
        self._value_labels = {}
        self._pending_cdb = []
        self.stats = {
            "raw_stalls": 0,
            "war_stalls": 0,
            "structural_stalls": 0,
        }

    # ── 执行一个时钟周期 ──

    def step(self):
        """执行单个时钟周期。

        流水线：Issue → Execute (L周期) → Write Result + CDB (1周期)。
        执行完成后推一周期才进入写结果，写结果与 CDB 广播在同一周期完成。
        依赖指令在 CDB 广播的下一周期才能拿到数据并执行。
        """
        self.cycle += 1
        self.done = False

        # 0. 将上一周期执行完成的条目标记为可写结果
        self._promote_exec_done()

        # 1. 执行阶段
        self._execute()

        # 2. 写结果阶段（入队）
        self._write_result()

        # 3. CDB 广播（与写结果同一周期）
        self._flush_cdb()

        # 4. 流出阶段
        self._issue()

        # 检查是否所有指令已完成
        self._check_done()

        # 保存历史快照
        self._save_history()

    # ── Issue 阶段 ──

    @staticmethod
    def _fp_name(name: str) -> str:
        """去除 FP 寄存器名称的 $ 前缀，统一为 'f0' 格式。"""
        return name.lstrip("$")

    def _issue(self):
        """尝试从指令队列取一条指令流出。"""
        if self.pc >= len(self.instructions):
            return

        inst = self.instructions[self.pc]

        if inst.is_load:
            self._issue_load(inst)
        elif inst.is_store:
            self._issue_store(inst)
        elif inst.is_fp_arith:
            self._issue_fp_arith(inst)

    def _issue_load(self, inst: Instruction):
        """流出 l.d 指令到 Load Buffer。"""
        # 找空闲 Load Buffer
        buf = self._find_free_load_buffer()
        if buf is None:
            self.stats["structural_stalls"] += 1
            self._log("stall", f"Load Buffer 全满，l.d 流出阻塞")
            return

        buf.busy = True
        buf.op = "l.d"
        buf.dest = self._fp_name(inst.rd)
        buf.inst_addr = inst.addr
        buf.remaining = self.latency.get("l.d")
        buf.base_reg = inst.rs
        buf.offset = inst.offset

        # 检查基址寄存器
        rs = inst.rs
        int_val = self.int_regs.read(rs)  # 整数寄存器总是就绪
        buf.vj = int_val
        buf.qj = None
        buf.address = int_val + inst.offset
        buf.address_ready = True

        # 更新目标寄存器状态
        self.register_status[self._fp_name(inst.rd)].qi = buf.name

        # 记录指令状态
        self._set_inst_status(inst.addr, issue=self.cycle)

        self._log("issue", f"l.d → {buf.name}, 地址=0x{buf.address:x}, 目标={inst.rd}")
        self.pc += 1

    def _issue_store(self, inst: Instruction):
        """流出 s.d 指令到 Store Buffer。"""
        buf = self._find_free_store_buffer()
        if buf is None:
            self.stats["structural_stalls"] += 1
            self._log("stall", f"Store Buffer 全满，s.d 流出阻塞")
            return

        buf.busy = True
        buf.op = "s.d"
        buf.inst_addr = inst.addr
        buf.remaining = self.latency.get("s.d")
        buf.base_reg = inst.rs
        buf.offset = inst.offset
        buf.store_src = inst.rt

        # 基址寄存器
        int_val = self.int_regs.read(inst.rs)
        buf.vj = int_val
        buf.qj = None
        buf.address = int_val + inst.offset
        buf.address_ready = True

        # 数据寄存器（FP）
        ft = self._fp_name(inst.rt)
        st = self.register_status[ft]
        if st.qi is not None:
            buf.qk = st.qi
            buf.vk = None
            buf.data_ready = False
        else:
            buf.qk = None
            buf.vk = st.value
            buf.data_ready = True

        # Store 不更新目标寄存器状态（Store 不写寄存器）

        self._set_inst_status(inst.addr, issue=self.cycle)

        self._log("issue", f"s.d → {buf.name}, 地址=0x{buf.address:x}, 数据来自 {ft}")
        self.pc += 1

    def _issue_fp_arith(self, inst: Instruction):
        """流出 FP 算术指令到对应保留站。"""
        # 确定使用哪种保留站
        if inst.op == "div.d":
            # 除法器复用 Mult 保留站（实现简化）
            rs_list = self.mul_rs
        elif inst.op == "mul.d":
            rs_list = self.mul_rs
        else:
            rs_list = self.add_rs

        free_rs = self._find_free_rs(rs_list)
        if free_rs is None:
            self.stats["structural_stalls"] += 1
            self._log("stall", f"{inst.op} 对应保留站全满，流出阻塞")
            return

        free_rs.busy = True
        free_rs.op = inst.op
        free_rs.dest = self._fp_name(inst.rd)
        free_rs.inst_addr = inst.addr
        free_rs.remaining = self.latency.get(inst.op)

        # 源操作数 1
        st_rs = self.register_status[self._fp_name(inst.rs)]
        if st_rs.qi is not None:
            free_rs.qj = st_rs.qi
            free_rs.vj = None
            free_rs.vj_label = None
        else:
            free_rs.qj = None
            free_rs.vj = st_rs.value
            free_rs.vj_label = st_rs.value_label

        # 源操作数 2
        st_rt = self.register_status[self._fp_name(inst.rt)]
        if st_rt.qi is not None:
            free_rs.qk = st_rt.qi
            free_rs.vk = None
            free_rs.vk_label = None
        else:
            free_rs.qk = None
            free_rs.vk = st_rt.value
            free_rs.vk_label = st_rt.value_label

        # 覆盖目标寄存器状态（寄存器重命名）
        rd = self._fp_name(inst.rd)
        self.register_status[rd].qi = free_rs.name
        self.register_status[rd].value = None
        self.register_status[rd].value_label = None

        self._set_inst_status(inst.addr, issue=self.cycle)

        self._log("issue", f"{inst.op} → {free_rs.name}, dest={inst.rd}, "
                  f"Qj={free_rs.qj or '就绪'}, Qk={free_rs.qk or '就绪'}")
        self.pc += 1

    # ── Execute 阶段 ──

    def _execute(self):
        """各功能部件执行/倒计数。write_ready 的条目在等待写结果，不参与执行。"""
        # 保留站执行
        all_rs = self.add_rs + self.mul_rs
        for rs in all_rs:
            if not rs.busy:
                continue
            if rs.write_ready:
                continue
            # 操作数未就绪 → RAW 等待
            if rs.qj is not None or rs.qk is not None:
                self.stats["raw_stalls"] += 1
                continue
            # 还未开始执行
            if rs.remaining == self.latency.get(rs.op):
                self._set_inst_status(rs.inst_addr, execute_start=self.cycle)
                self._log("execute", f"{rs.op} → {rs.name} 开始执行 ({rs.remaining} 周期)")
            # 倒计数
            if rs.remaining > 0:
                rs.remaining -= 1
            # 执行完成
            if rs.remaining == 0:
                self._set_inst_status(rs.inst_addr, execute_end=self.cycle)
                rs.exec_done = True

        # Load Buffer 执行
        for buf in self.load_buffers:
            if not buf.busy:
                continue
            if buf.write_ready:
                continue
            if not buf.address_ready:
                continue
            # 开始执行
            if buf.remaining == self.latency.get("l.d"):
                self._set_inst_status(buf.inst_addr, execute_start=self.cycle)
                self._log("execute", f"l.d → {buf.name} 开始执行 ({buf.remaining} 周期)")
            if buf.remaining > 0:
                buf.remaining -= 1
            if buf.remaining == 0:
                self._set_inst_status(buf.inst_addr, execute_end=self.cycle)
                buf.exec_done = True

        # Store Buffer 执行（需要地址 + 数据都就绪，且无更早未完成 Store）
        for buf in self.store_buffers:
            if not buf.busy:
                continue
            if buf.write_ready:
                continue
            if not buf.address_ready:
                continue
            # 数据未就绪 → RAW 等待
            if not buf.data_ready:
                self.stats["raw_stalls"] += 1
                continue
            # 检查是否有更早的未完成 Store
            if self._has_earlier_store(buf):
                continue
            if buf.remaining == self.latency.get("s.d"):
                self._set_inst_status(buf.inst_addr, execute_start=self.cycle)
            if buf.remaining > 0:
                buf.remaining -= 1
            if buf.remaining == 0:
                self._set_inst_status(buf.inst_addr, execute_end=self.cycle)
                buf.exec_done = True

    # ── Write Result 阶段（入队，下一周期才广播）──

    def _write_result(self):
        """收集本周期可写结果的生产者，入队后由 _flush_cdb 在同一周期广播。"""
        # FP 保留站
        for rs in self.add_rs + self.mul_rs:
            if rs.busy and rs.write_ready and rs.op is not None:
                self._set_inst_status(rs.inst_addr, write_result=self.cycle)
                self._pending_cdb.append(("rs", rs))

        # Load Buffer
        for buf in self.load_buffers:
            if buf.busy and buf.write_ready and buf.op is not None:
                self._set_inst_status(buf.inst_addr, write_result=self.cycle)
                self._pending_cdb.append(("load", buf))

        # Store Buffer
        for buf in self.store_buffers:
            if buf.busy and buf.write_ready and buf.op is not None:
                self._set_inst_status(buf.inst_addr, write_result=self.cycle)
                self._pending_cdb.append(("store", buf))

    # ── Flush CDB（下一周期开始时执行）──

    def _flush_cdb(self):
        """冲刷上一周期排队的 CDB 广播。"""
        for item in self._pending_cdb:
            kind, obj = item
            if kind == "rs":
                self._cdb_broadcast_rs(obj)
            elif kind == "load":
                self._cdb_broadcast_load(obj)
            elif kind == "store":
                self._complete_store(obj)
        self._pending_cdb.clear()

    def _cdb_broadcast_rs(self, rs: ReservationEntry):
        """保留站完成执行的 CDB 广播。"""
        tag = rs.name
        self._value_counter += 1
        self._value_labels[tag] = f"M{self._value_counter}"
        result = self._compute_result(rs)

        self._broadcast(tag, result)

        # 写目标寄存器（检查 WAW）
        dest = self._fp_name(rs.dest) if rs.dest else None
        if dest and self.register_status[dest].qi == tag:
            self.register_status[dest].qi = None
            self.register_status[dest].value = result
            self.register_status[dest].value_label = self._value_labels.get(tag, tag)
            # 构建计算表达式
            op_symbol = {"add.d": "+", "sub.d": "-", "mul.d": "*", "div.d": "/"}.get(rs.op, "?")
            vj_str = rs.vj_label or (f"R[{rs.dest}]" if rs.dest else str(rs.vj or 0))
            vk_str = rs.vk_label or (f"R[{rs.dest}]" if rs.dest else str(rs.vk or 0))
            self.register_status[dest].computation = f"{vj_str} {op_symbol} {vk_str}"
        # 如果 qi 不匹配（WAW），不写寄存器

        self._log("cdb", f"CDB: {tag} → {rs.dest}={result}")
        rs.clear()

    def _cdb_broadcast_load(self, buf: LoadStoreEntry):
        """Load Buffer 完成执行的 CDB 广播。"""
        tag = buf.name
        self._value_counter += 1
        self._value_labels[tag] = f"M{self._value_counter}"
        # 从内存读取数据
        result = self.data_mem.read_double(buf.address)

        self._broadcast(tag, result)

        # 写目标寄存器（检查 WAW）
        dest = self._fp_name(buf.dest) if buf.dest else None
        value_label = self._value_labels.get(tag)
        if dest and self.register_status[dest].qi == tag:
            self.register_status[dest].qi = None
            self.register_status[dest].value = result
            if value_label:
                self.register_status[dest].value_label = value_label
                self.register_status[dest].computation = (
                    f"MEM[R{buf.base_reg.lstrip('Rr')}+{buf.offset}]"
                )

        self._log("cdb", f"CDB: {tag} → {dest}={result}")
        buf.clear()

    def _broadcast(self, tag: str, value: float):
        """CDB 广播：将 tag + value 传播到所有等待该 tag 的保留站和 Buffer。"""
        value_label = self._value_labels.get(tag)
        # 遍历所有保留站
        for rs in self.add_rs + self.mul_rs:
            if rs.busy and rs.qj == tag:
                rs.qj = None
                rs.vj = value
                if value_label:
                    rs.vj_label = value_label
            if rs.busy and rs.qk == tag:
                rs.qk = None
                rs.vk = value
                if value_label:
                    rs.vk_label = value_label

        # 遍历 Store Buffer
        for buf in self.store_buffers:
            if buf.busy and buf.qk == tag:
                buf.qk = None
                buf.vk = value
                buf.data_ready = True

    def _complete_store(self, buf: LoadStoreEntry):
        """Store 完成：写内存。"""
        # Store 完成，写数据到内存
        self.data_mem.write_double(buf.address, buf.vk)
        self._log("cdb", f"Store: {buf.name} → MEM[0x{buf.address:x}] = {buf.vk}")
        buf.clear()

    def _compute_result(self, rs: ReservationEntry) -> float:
        """根据保留站的操作码和操作数计算结果。"""
        op = rs.op
        vj = rs.vj or 0.0
        vk = rs.vk or 0.0
        if op == "add.d":
            return vj + vk
        elif op == "sub.d":
            return vj - vk
        elif op == "mul.d":
            return vj * vk
        elif op == "div.d":
            return vj / vk if vk != 0 else float("inf")
        return 0.0

    # ── 执行完成 → 可写结果 推一周期 ──

    def _promote_exec_done(self):
        """将上一周期执行完成的条目（exec_done）提升为可写结果（write_ready）。"""
        for rs in self.add_rs + self.mul_rs:
            if rs.busy and rs.exec_done:
                rs.exec_done = False
                rs.write_ready = True
        for buf in self.load_buffers:
            if buf.busy and buf.exec_done:
                buf.exec_done = False
                buf.write_ready = True
        for buf in self.store_buffers:
            if buf.busy and buf.exec_done:
                buf.exec_done = False
                buf.write_ready = True

    # ── 辅助方法 ──

    def _find_free_rs(self, rs_list: list[ReservationEntry]) -> Optional[ReservationEntry]:
        for rs in rs_list:
            if not rs.busy:
                return rs
        return None

    def _find_free_load_buffer(self) -> Optional[LoadStoreEntry]:
        for buf in self.load_buffers:
            if not buf.busy:
                return buf
        return None

    def _find_free_store_buffer(self) -> Optional[LoadStoreEntry]:
        for buf in self.store_buffers:
            if not buf.busy:
                return buf
        return None

    def _has_earlier_store(self, target: LoadStoreEntry) -> bool:
        """检查是否有比 target 更早（指令地址更小）的未完成 Store。"""
        for buf in self.store_buffers:
            if buf is target:
                continue
            if buf.busy and buf.inst_addr is not None and target.inst_addr is not None:
                if buf.inst_addr < target.inst_addr:
                    return True
        return False

    def _set_inst_status(self, addr: int, **kwargs):
        """更新指令状态表中对应指令的字段。"""
        for entry in self.instruction_status:
            if entry.addr == addr:
                for key, value in kwargs.items():
                    setattr(entry, key, value)
                break

    def _check_done(self):
        """检查所有指令是否已完成。"""
        if self.pc >= len(self.instructions):
            all_done = True
            for entry in self.instruction_status:
                if not entry.done:
                    # 检查 Write Result 是否已完成
                    if entry.write_result is not None:
                        entry.done = True
                    else:
                        all_done = False
            # 同时检查所有功能部件空闲
            all_idle = True
            for rs in self.add_rs + self.mul_rs:
                if rs.busy:
                    all_idle = False
            for buf in self.load_buffers + self.store_buffers:
                if buf.busy:
                    all_idle = False
            if all_done and all_idle:
                self.done = True

    def _log(self, event_type: str, message: str):
        self.events.append(Event(cycle=self.cycle, type=event_type, message=message))

    def _save_history(self):
        """保存当前周期完整快照。"""
        self.history.append(self.to_snapshot())

    # ── 获取历史快照 ──

    def get_history(self, cycle: int) -> Optional[dict]:
        """返回指定周期的快照。"""
        if 0 <= cycle - 1 < len(self.history):
            return self.history[cycle - 1]
        return None

    # ── 导出完整状态快照 ──

    def to_snapshot(self) -> dict:
        """导出当前模拟状态为 JSON 兼容的 dict。"""
        # 计算统计
        completed = sum(1 for e in self.instruction_status if e.done)
        total_cycles = self.cycle

        return {
            "cycle": self.cycle,
            "pc": self.pc,
            "running": self.running,
            "done": self.done,
            "registers": {
                "integer": {
                    str(k): v for k, v in sorted(self.int_regs.regs.items(), key=lambda x: int(x[0]))
                },
            },
            "register_status": {
                name: self.register_status[name].to_dict()
                for name in FP_REGISTERS
            },
            "reservation_stations": {
                "fp_add": [rs.to_dict() for rs in self.add_rs],
                "fp_mul": [rs.to_dict() for rs in self.mul_rs],
            },
            "loadstore_buffers": (
                [buf.to_dict() for buf in self.load_buffers]
                + [buf.to_dict() for buf in self.store_buffers]
            ),
            "instruction_status": [e.to_dict() for e in self.instruction_status],
            "instructions": [
                {"addr": inst.addr, "text": inst.text}
                for inst in self.instructions
            ],
            "data_memory": self.data_mem.to_dict(),
            "events": [
                f"[Cycle {ev.cycle}] {'Issue' if ev.type == 'issue' else 'Execute' if ev.type == 'execute' else 'CDB' if ev.type == 'cdb' else 'Stall'}: {ev.message}"
                for ev in self.events[-20:]  # 只保留最近 20 条
            ],
            "history": True,
            "max_history_cycle": self.cycle,
            "stats": {
                "total_cycles": total_cycles,
                "completed_instructions": completed,
                "raw_stalls": self.stats["raw_stalls"],
                "war_stalls": self.stats["war_stalls"],
                "structural_stalls": self.stats["structural_stalls"],
                "cpi": round(total_cycles / completed, 2) if completed > 0 else 0,
            },
        }
