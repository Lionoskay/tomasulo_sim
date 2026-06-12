# 模拟器A — Tomasulo 动态调度模拟器 工作方案

## 技术选型

| 项目 | 选择 | 理由 |
|------|------|------|
| 后端 | Python 3 + Flask | 轻量 Web 框架，与五段流水线项目技术栈一致 |
| 前端 | 单个 HTML 文件（原生 HTML/CSS/JS） | 无框架依赖，表格+按钮+CSS 即可实现全部交互 |
| 运行方式 | `python app.py` 本地启动 | 轻量，无需容器化 |
| 体系结构 | MIPS 浮点指令子集 | 浮点 load/store/add/sub/mul/div |

---

## 项目文件结构

```
tomasulo/
├── requirements.txt          # Python 依赖：flask
├── app.py                    # Flask 入口：API 路由 + 启动服务器
├── tomasulo.py               # Tomasulo 核心：流出/执行/写结果三阶段 + CDB
├── datapath.py               # 数据通路：浮点寄存器堆、内存、功能部件
├── assembler.py              # 汇编器：MIPS 浮点指令文本 → 内部表示
├── templates/
│   └── index.html            # 前端页面：状态表 + 保留站 + 交互控制
└── test_programs/
    ├── no_conflict.txt       # 场景1：无冲突
    ├── raw_conflict.txt      # 场景2：RAW冲突
    └── war_conflict.txt      # 场景3：WAR+RAW冲突
```

---

## 架构概览

```
浏览器                              Flask 服务（本地）
┌───────────────────┐    HTTP       ┌──────────────────────────┐
│  index.html       │ ←──────────→  │  Flask (app.py)          │
│  HTML/CSS/JS      │   JSON API    │      ↓                   │
│                   │               │  tomasulo.py             │
│  状态表+保留站     │               │      ↓                   │
│  命令交互          │               │  datapath.py             │
│                   │               │  assembler.py            │
└───────────────────┘               └──────────────────────────┘
```

前后端通过 REST API 通信，数据格式为 `SimulationSnapshot` JSON。

---

## Tomasulo 算法核心概念

### 三阶段流程

```
        ┌──────────┐      ┌──────────┐      ┌──────────┐
        │  Issue   │ ───→ │ Execute  │ ───→ │  Write   │
        │  流出    │      │  执行    │      │  Result  │
        └──────────┘      └──────────┘      │  写结果  │
             │                  │            └──────────┘
             ↓                  ↓                 │
        从指令队列    功能部件按延迟       CDB 广播结果
        取指令，分配   周期执行，监听      到保留站+寄存器
        保留站/缓冲   CDB 获取操作数
```

### 核心数据结构

| 结构 | 说明 |
|------|------|
| **保留站 (Reservation Station)** | FP Add（3个）、FP Mul（2个），记录 剩余周期/Busy/Op/Vj/Vk/Qj/Qk/目标寄存器 |
| **Load/Store Buffer** | Load×3 + Store×3，合并管理，记录有效地址、值、剩余周期、目标寄存器 |
| **寄存器结果状态 (Register Status)** | 16 个 FP 寄存器（F0-F30），每个记录 Qi（生产者 tag）和当前值 |
| **指令状态表 (Instruction Status)** | 每条指令记录 Issue / 执行开始 / 执行结束 / Write Result 时钟周期 |
| **功能部件** | 延迟由用户在前端配置，默认 Load=2 / Store=1 / ADD=2 / SUB=2 / MUL=10 / DIV=40 |


---

## Step 1：数据通路基础（datapath.py）

**目标**：搭建浮点运算硬件组件

### 实现内容
- **浮点寄存器堆**：16 个 64 位双精度 FP 寄存器（`$f0`, `$f2`, `$f4` … `$f30`，仅偶数编号）
- **通用寄存器堆**（整数）：32 个 32 位寄存器（`R0` ~ `R31`），`R0` 硬连线为 0，用于 load/store 地址计算
- **数据内存**：数组模拟，支持 `l.d`/`s.d`（按双字存取）
- **指令内存**：数组模拟，按地址索引
- **PC**：程序计数器
- **功能部件**（延迟由用户在前端自行设置，以下为默认值）：
  - Load 部件：默认 2 周期
  - Store 部件：默认 1 周期
  - FP 加法器：默认 2 周期
  - FP 乘法器：默认 10 周期
  - FP 除法器：默认 40 周期

### 验证标准
- 能读写 FP 寄存器和整数寄存器
- 功能部件周期计数正确

---

## Step 2：指令集支持（assembler.py + tomasulo.py 骨架）

**目标**：支持 6 条浮点指令的解析

### 实现内容

| 指令 | 格式 | 含义 |
|------|------|------|
| `l.d` | `l.d $ft, offset(Rs)` | 从内存加载双字到 FP 寄存器 |
| `s.d` | `s.d $ft, offset(Rs)` | 将 FP 寄存器值存入内存 |
| `add.d` | `add.d $fd, $fs, $ft` | FP 加法，延迟 2 周期 |
| `sub.d` | `sub.d $fd, $fs, $ft` | FP 减法，延迟 2 周期 |
| `mul.d` | `mul.d $fd, $fs, $ft` | FP 乘法，延迟 10 周期 |
| `div.d` | `div.d $fd, $fs, $ft` | FP 除法，延迟 40 周期 |

### 验证标准
- 6 条指令解析正确
- 单条指令在无流水线模式下能正确执行

---

## Step 3：Tomasulo 核心三阶段（tomasulo.py）

**目标**：实现 Issue → Execute → Write Result 闭环

### 3.1 流出阶段（Issue）

每个时钟周期尝试从指令队列取一条指令流出：

**条件检查：**
- 对应功能部件的保留站有空闲项（或 Load/Store Buffer 有空闲项）
- 不存在结构冲突

**流出动作：**
- 分配保留站/缓冲器，设置 Busy=1, Op=操作码
- 读取寄存器结果状态表，设置 Qj/Qk（等待的 tag）或 Vj/Vk（就绪的值）
- 目标寄存器状态更新为该保留站的 tag（覆盖之前状态）
- 指令从队列移除

**Load/Store 特殊处理：**
- Load：分配 Load Buffer，记录基址寄存器等待状态
- Store：分配 Store Buffer，记录基址和数据等待状态

### 3.2 执行阶段（Execute）

每个时钟周期，各功能部件检查自身保留站：

**执行条件：**
- 保留站 Busy=1 且 Qj=null 且 Qk=null（两个操作数都就绪）
- Load Buffer：基址寄存器已就绪（Qj=null），可计算有效地址
- Store Buffer：基址寄存器已就绪且待存数据已就绪（Qj=null 且 Qk=null），且无更早的未完成 Store 在前

**执行动作：**
- 功能部件占用，剩余周期数 = 操作延迟
- 每周期递减，减到 0 时进入写结果阶段
- Load/Store 在地址计算完成后进入相应的有效地址阶段

**写结果条件：**
- 功能部件完成执行（剩余周期 = 0）
- CDB 可用（实际实现中可简化：每周期允许多个写结果）

### 3.3 写结果阶段（Write Result）

**写结果动作：**
- 通过 CDB 广播：tag + 计算结果值
- 遍历所有保留站、Load Buffer、Store Buffer：
  - 若 Qj == 本 tag，则清除 Qj 并将值写入 Vj
  - 若 Qk == 本 tag，则清除 Qk 并将值写入 Vk
- 若寄存器结果状态表中对应寄存器的 tag 匹配，将值写入寄存器，清除状态为 "就绪"
- 释放该保留站/缓冲器（Busy=0）

### 验证标准
- 单条指令经历 Issue → Execute → Write Result 完整流程
- 保留站正确分配和释放
- 寄存器结果状态表正确更新

---

## Step 4：数据冲突处理（tomasulo.py 扩展）

**目标**：正确演示 RAW、WAR、WAW 冲突通过 Tomasulo 算法自动解决

### RAW 冲突（Read After Write）

- **场景**：`mul.d $f0, $f2, $f4` → `add.d $f6, $f0, $f8`
- **Tomasulo 处理**：
  - add.d 流出时检查寄存器状态表，发现 `$f0` 由 Mult1 生产
  - add.d 的保留站记录 Qj=Mult1，Vj 为空
  - Mult1 写结果时通过 CDB 广播，add.d 的 Qj 被清除，Vj 获得值
  - add.d 操作数就绪后开始执行

### WAR 冲突（Write After Read）

- **场景**：`add.d $f0, $f2, $f4` → `mul.d $f2, $f6, $f8`（先读 `$f2`，后写 `$f2`）
- **Tomasulo 处理**：
  - add.d 流出时读取 `$f2`（此时 `$f2` 已就绪），Vj 直接获得值
  - mul.d 流出时覆盖 `$f2` 的寄存器状态为 Mult1
  - WAR 冲突通过寄存器重命名自然解决——add.d 已经拿到了旧值

### WAW 冲突（Write After Write）

- **场景**：`mul.d $f0, $f2, $f4`（10 周期）→ `add.d $f0, $f6, $f8`（2 周期）
- **Tomasulo 处理**：
  - mul.d 流出，`$f0` 状态 → Mult1
  - add.d 流出，`$f0` 状态 → Add1（覆盖）
  - add.d 先完成写结果，`$f0` = add.d 结果
  - mul.d 后完成写结果时，检查寄存器状态表——`$f0` 的 tag 是 Add1 不是 Mult1
  - mul.d **不写寄存器**（WAW 由最后一条写指令控制寄存器写入）

### 验证标准
- RAW：依赖指令正确等待生产者完成
- WAR：先读的指令已取到旧值，后写的指令不干扰
- WAW：最后写回的指令控制寄存器值，先流出但后完成的指令不覆盖

---

## Step 5：Flask API 层（app.py）

**目标**：将 Tomasulo 引擎封装为 REST API

### API 路由

| 方法 | 路径 | 功能 | 请求体 |
|------|------|------|--------|
| `GET` | `/api/state` | 获取当前完整状态 | — |
| `POST` | `/api/step` | 单步执行 1 个周期 | `{ "cycles": 1 }` |
| `POST` | `/api/step` | 连续执行 N 个周期 | `{ "cycles": 5 }` |
| `POST` | `/api/run` | 执行到程序结束 | — |
| `POST` | `/api/pause` | 暂停连续执行 | — |
| `POST` | `/api/reset` | 重置模拟器 | — |
| `POST` | `/api/load` | 载入程序 + 设置延迟 | `{ "code": "...", "latencies": {"load":2,"store":1,"add":2,"sub":2,"mul":10,"div":40} }` |
| `POST` | `/api/history` | 回看历史周期的系统状态 | `{ "cycle": 12 }` |
| `GET` | `/` | 返回 index.html 页面 | — |

### 返回数据格式

```json
{
  "cycle": 8,
  "pc": 20,
  "running": false,
  "registers": {
    "integer": {"0": 0, "1": 100}
  },
  "register_status": {
    "f0":  {"qi": null,     "value": 0.0},
    "f2":  {"qi": null,     "value": 3.14},
    "f4":  {"qi": null,     "value": 2.71},
    "f6":  {"qi": "Mult1",  "value": null},
    "f8":  {"qi": "Add1",   "value": null},
    "f10": {"qi": null,     "value": 0.0},
    "f12": {"qi": null,     "value": 0.0},
    "f14": {"qi": null,     "value": 0.0},
    "f16": {"qi": null,     "value": 0.0},
    "f18": {"qi": null,     "value": 0.0},
    "f20": {"qi": null,     "value": 0.0},
    "f22": {"qi": null,     "value": 0.0},
    "f24": {"qi": null,     "value": 0.0},
    "f26": {"qi": null,     "value": 0.0},
    "f28": {"qi": null,     "value": 0.0},
    "f30": {"qi": null,     "value": 0.0}
  },
  "reservation_stations": {
    "fp_add": [
      {"name": "Add1", "busy": true,  "op": "add.d", "vj": 3.14, "vk": 2.71, "qj": null, "qk": null, "remaining": 0, "dest": "f8"},
      {"name": "Add2", "busy": false, "op": null,    "vj": null, "vk": null, "qj": null, "qk": null, "remaining": 0, "dest": null},
      {"name": "Add3", "busy": false, "op": null,    "vj": null, "vk": null, "qj": null, "qk": null, "remaining": 0, "dest": null}
    ],
    "fp_mul": [
      {"name": "Mult1", "busy": true,  "op": "mul.d", "vj": 3.14, "vk": 2.71, "qj": null, "qk": null, "remaining": 7, "dest": "f6"},
      {"name": "Mult2", "busy": false, "op": null,    "vj": null, "vk": null, "qj": null, "qk": null, "remaining": 0, "dest": null}
    ]
  },
  "loadstore_buffers": [
    {"name": "Load1",  "busy": false, "op": null,  "address": null, "value": null, "remaining": 0, "dest": null},
    {"name": "Load2",  "busy": false, "op": null,  "address": null, "value": null, "remaining": 0, "dest": null},
    {"name": "Load3",  "busy": false, "op": null,  "address": null, "value": null, "remaining": 0, "dest": null},
    {"name": "Store1", "busy": true,  "op": "s.d", "address": 116,  "value": null, "remaining": 0, "dest": null},
    {"name": "Store2", "busy": false, "op": null,  "address": null, "value": null, "remaining": 0, "dest": null},
    {"name": "Store3", "busy": false, "op": null,  "address": null, "value": null, "remaining": 0, "dest": null}
  ],
  "instruction_status": [
    {"addr": 0,  "text": "l.d   $f2, 0(R1)",    "issue": 1, "execute_start": 1, "execute_end": 2, "write_result": 3, "done": true},
    {"addr": 4,  "text": "l.d   $f4, 8(R1)",    "issue": 2, "execute_start": 2, "execute_end": 3, "write_result": 4, "done": true},
    {"addr": 8,  "text": "mul.d $f6, $f2, $f4", "issue": 3, "execute_start": 5, "execute_end": null, "write_result": null, "done": false},
    {"addr": 12, "text": "add.d $f8, $f2, $f4", "issue": 4, "execute_start": 5, "execute_end": 7, "write_result": null, "done": false},
    {"addr": 16, "text": "s.d   $f8, 16(R1)",   "issue": 5, "execute_start": null, "execute_end": null, "write_result": null, "done": false}
  ],
  "instructions": [
    {"addr": 0,  "text": "l.d   $f2, 0(R1)"},
    {"addr": 4,  "text": "l.d   $f4, 8(R1)"},
    {"addr": 8,  "text": "mul.d $f6, $f2, $f4"},
    {"addr": 12, "text": "add.d $f8, $f2, $f4"},
    {"addr": 16, "text": "s.d   $f8, 16(R1)"}
  ],
  "data_memory": {"100": 3.14, "108": 2.71},
  "events": [
    "[Cycle 3] Issue: mul.d → Mult1, Qj=Load1 (f2), Qk=Load2 (f4)",
    "[Cycle 3] Write Result: Load1 → CDB broadcast $f2=3.14, Mult1.Vj ready",
    "[Cycle 4] Write Result: Load2 → CDB broadcast $f4=2.71, Mult1.Vk ready, Add1.Vk ready",
    "[Cycle 5] Issue: s.d → Store1, address=116, waiting for $f8 from Add1",
    "[Cycle 5] Execute start: Mult1 (10 cycles), Add1 (2 cycles)"
  ],
  "history": true,
  "max_history_cycle": 8,
  "stats": {
    "total_cycles": 8,
    "completed_instructions": 2,
    "raw_stalls": 0,
    "war_stalls": 0,
    "structural_stalls": 0
  }
}
```

### 历史回看机制

- 每个周期结束时，将完整快照存入 `history[cycle]` 列表
- `POST /api/history { "cycle": N }` 返回第 N 周期的快照
- 前端提供滑块或数字输入框选择历史周期
- 回看模式下所有面板显示历史状态（只读，不可单步）

### 验证标准
- `GET /api/state` 返回正确 JSON
- `POST /api/step` 后 cycle+1
- `POST /api/history` 返回正确历史快照
- `POST /api/load` 支持文件内容载入

---

## Step 6：Web 前端仪表盘（templates/index.html）

**目标**：单个 HTML 文件，浅色主题，左侧设置面板 + 右侧执行状态面板

**配色**：浅色主题，白底 + 浅灰边框 + 蓝/绿/橙强调色

---

### 6.1 页面整体布局（左-右两栏）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Tomasulo 动态调度模拟器                                                     │
├──────────────────────────┬───────────────────────────────────────────────────┤
│                          │                                                   │
│   左侧：设置面板         │   右侧：执行状态面板                               │
│   (约 30% 宽度)          │   (约 70% 宽度)                                    │
│                          │                                                   │
│  ┌─ 指令输入 ──────────┐│  ┌─ 指令状态表 ─────────────────────────────────┐ │
│  │ 文件输入:            ││  │ 地址 │ 指令               │Issue│始│末│写结果│完成│ │
│  │ [选择文件] [预设▼]  ││  │ 0x00 │ l.d   $f2, 0(R1)  │  1  │ 1│ 2│  3   │ ✓ │ │
│  │                      ││  │ 0x04 │ l.d   $f4, 8(R1)  │  2  │ 2│ 3│  4   │ ✓ │ │
│  │ 文本输入:            ││  │ 0x08 │ mul.d $f6, $f2, $f4│  3  │ 5│ —│  —   │ — │ │
│  │ ┌──────────────────┐ ││  │ 0x0C │ add.d $f8, $f2, $f4│  4  │ 5│ 7│  —   │ — │ │
│  │ │ l.d   $f2,0(R1)  │ ││  │ 0x10 │ s.d   $f8, 16(R1)│  5  │ —│ —│  —   │ — │ │
│  │ │ l.d   $f4,8(R1)  │ ││  └────────────────────────────────────────────┘ │
│  │ │ mul.d $f6,$f2,$f4 │ ││                                                   │
│  │ │ add.d $f8,$f2,$f4 │ ││  ┌─ Load/Store 部件状态表 ─────────────────────┐ │
│  │ │ s.d   $f8,16(R1)  │ ││  │Name│Busy│Op │有效地址│值   │剩余│目标寄存器│ │
│  │ └──────────────────┘ ││  │ Ld1│ N  │ — │  —    │ —   │ 0  │  —      │ │
│  │                      ││  │ Ld2│ N  │ — │  —    │ —   │ 0  │  —      │ │
│  │ [载入] [清空]        ││  │ Ld3│ N  │ — │  —    │ —   │ 0  │  —      │ │
│  │                      ││  │ St1│ Y  │s.d│ 0x74  │ —   │ 0  │  —      │ │
│  │ 载入状态: ✓ 已加载5条││  │ St2│ N  │ — │  —    │ —   │ 0  │  —      │ │
│  │        或 ✗ 第3行错误││  │ St3│ N  │ — │  —    │ —   │ 0  │  —      │ │
│  ┌─ 功能部件延迟设置 ──┐││  └────────────────────────────────────────────┘ │
│  │ Load:  [ 2 ] 周期   │││                                                   │
│  │ Store: [ 1 ] 周期   │││  ┌─ 保留站状态表 ──────────────────────────────┐ │
│  │ ADD:   [ 2 ] 周期   │││  │剩余│Name │Busy│Op   │Vj  │Vk  │Qj│Qk│目标│ │
│  │ SUB:   [ 2 ] 周期   │││  │ 0  │Add1 │ Y  │add.d│3.14│2.71│ —│ —│$f8 │ │
│  │ MUL:   [10] 周期    │││  │ 0  │Add2 │ N  │ —   │ —  │ —  │ —│ —│ —  │ │
│  │ DIV:   [40] 周期    │││  │ 0  │Add3 │ N  │ —   │ —  │ —  │ —│ —│ —  │ │
│  └─────────────────────┘││  │ 7  │Mult1│ Y  │mul.d│3.14│2.71│ —│ —│$f6 │ │
│                          ││  │ 0  │Mult2│ N  │ —   │ —  │ —  │ —│ —│ —  │ │
│  ┌─ 运行控制 ──────────┐││  └────────────────────────────────────────────┘ │
│  │ [单步] [▶ 运行]     │││                                                   │
│  │ [暂停] [重置]        │││  ┌─ 寄存器状态表 (F0-F30，共16个) ───────────┐  │
│  │                      │││  │          │$f0│$f2│$f4│$f6  │$f8  │…│$f30│  │
│  │ 速度: [====○====]    │││  │  Qi      │ — │ — │ — │Mult1│Add1 │…│ —  │  │
│  │                      │││  │  值      │0.0│3.14│2.71│ —  │ —  │…│0.0 │  │
│  │ 跳转到周期: [___]    │││  └────────────────────────────────────────────┘  │
│  │ [跳转] [回到最新]    │││                                                   │
│  └─────────────────────┘││  ┌─ 事件日志 ─────────────────────────────────┐  │
│                          ││  │ [Cycle 3] Issue: mul.d → Mult1            │  │
│                          ││  │ [Cycle 3] CDB: Load1 → $f2=3.14           │  │
│                          ││  │ [Cycle 4] CDB: Load2 → $f4=2.71           │  │
│                          ││  │ [Cycle 5] Issue: s.d → Store1, addr=0x74  │  │
│                          ││  └────────────────────────────────────────────┘  │
│                          ││                                                   │
│                          ││  性能: 总周期 8 | 完成 2 | CPI 4.0                │
├──────────────────────────┴───────────────────────────────────────────────────┤
│  当前周期: 8    状态: ○ 已暂停                                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

### 6.2 指令状态表

**布局：** HTML `<table>`，列：`地址 | 指令 | Issue | 执行开始 | 执行结束 | 写结果 | 完成`

**交互特性：**
- 每一行对应一条指令，显示其 Issue / 执行开始 / 执行结束 / 写结果 发生的时钟周期
- 当前周期正在变化的单元格高亮闪烁
- 尚未发生的阶段显示 `—`
- 已完成指令整行淡化（opacity: 0.4）
- 已完成的指令在"完成"列显示 `✓`

**每行颜色规则（按指令当前所处阶段）：**
- Issue 阶段：淡黄色背景
- Execute 阶段（执行中）：淡橙色背景
- Write Result 阶段：淡绿色背景
- 完成：灰色文字 + 删除线效果

---

### 6.3 Load/Store 部件状态表（合并表）

**布局：** HTML `<table>`，列：`Name | Busy | Op | 有效地址 | 值 | 剩余周期 | 目标寄存器`

**说明：**
- Load Buffer（3 个）、Store Buffer（3 个）合并在同一表格中，按名称排序
- 不显示 Qj/Qk（内部实现细节，前端不暴露）
- Load 行的"值"列始终为 `—`（Load 结果通过 CDB 直接写入寄存器，不在 Load Buffer 中暂存）
- Store 行的"目标寄存器"列始终为 `—`

**交互特性：**
- Busy=Y 的行淡蓝背景，Busy=N 的灰色淡化
- 有效地址已计算的显示十六进制值（如 `0x64`），未计算的显示 `—`
- 剩余周期倒计数实时更新

---

### 6.4 保留站状态表

**布局：** HTML `<table>`，列：`剩余周期 | Name | Busy | Op | Vj | Vk | Qj | Qk | 目标寄存器`

**说明：**
- 最左侧列为该保留站的剩余执行时间（倒计数），是最关键的实时信息
- ADD 保留站（Add1~Add3）和 MUL 保留站（Mult1~Mult2）合并为一个表格
- DIV 指令使用独立的除法保留站（与 MUL 分离），按其自身的除法延迟（默认 40 周期）执行
- Qj/Qk 显示等待的保留站 tag，非空时橙色标识

**交互特性：**
- Busy=Y 的行淡蓝背景
- Qj/Qk 非空时单元格橙色背景（等待中）
- Qj/Qk 被 CDB 清零时单元格闪烁绿色
- 剩余周期为 0 时进入写结果阶段，边框发光

---

### 6.5 寄存器状态表（转置布局）

**布局：** HTML `<table>`，**3 行 × 17 列**（1 列表头 + 16 个寄存器列）

```
┌──────┬─────┬─────┬─────┬─────┬─────┬───┬──────┐
│      │ $f0 │ $f2 │ $f4 │ $f6 │ $f8 │…  │ $f30 │
├──────┼─────┼─────┼─────┼─────┼─────┼───┼──────┤
│  Qi  │  —  │ Ld1 │Add1 │Mul1 │  —  │…  │  —   │
├──────┼─────┼─────┼─────┼─────┼─────┼───┼──────┤
│  值  │3.14 │  —  │  —  │  —  │2.71 │…  │ 0.0  │
└──────┴─────┴─────┴─────┴─────┴─────┴───┴──────┘
```

- 第 1 行：寄存器名（`$f0`, `$f2`, `$f4` … `$f30`）
- 第 2 行：Qi（Tag），`—` 表示值已就绪，非空显示蓝色 tag 标签
- 第 3 行：值，Qi 非空时显示 `—`，Qi 为空时显示实际双精度浮点值

**交互特性：**
- Qi 非空的列：Qi 格蓝色标签，值格显示 `—`
- Qi 为空的列：Qi 格显示 `—`（绿色），值格显示实际数值
- CDB 写回时：对应列的 Qi 格从蓝色 tag → `—`，值格从 `—` → 数值，整列闪烁黄色
- 仅显示 F0-F30 共 16 个偶数编号寄存器

---

### 6.6 左侧设置面板

#### 指令输入（支持文件 + 文本双模式）

- **[选择文件]** 按钮：`<input type="file" accept=".txt">`，选中后自动读取文件内容填充到文本框
- **[预设文件▼]** 下拉菜单：列出 `test_programs/` 目录下的文件，选中后加载到文本框
- **文本输入区**：`<textarea>`（等宽字体，约 10 行 × 40 列），用户可直接输入/粘贴指令
- **[载入]** 按钮：将文本框内容发送到后端解析
  - 载入前校验：所有功能部件延迟必须已设置
  - 解析成功后更新所有状态面板，文本框上方显示 `✓ 已加载 N 条指令`
  - 解析失败红色提示，标出错误行号
- **[清空]** 按钮：清空文本框

**指令格式：**
```
l.d   $f0, 0(R1)
l.d   $f2, 8(R1)
mul.d $f4, $f0, $f2
add.d $f6, $f0, $f2
s.d   $f6, 16(R1)
```
- 每行一条指令，空行和 `#` 注释忽略
- 寄存器仅支持偶数编号：`$f0, $f2, $f4 … $f30`
- 立即数 offset 支持十进制整数

#### 功能部件延迟设置

在载入程序之前，用户必须配置各功能部件的执行周期数。所有部件均有默认值。

| 部件 | 参数名 | 默认值 | 输入控件 |
|------|--------|--------|---------|
| Load | `load_latency` | 2 | `<input type="number" min="1" value="2">` |
| Store | `store_latency` | 1 | `<input type="number" min="1" value="1">` |
| ADD | `add_latency` | 2 | `<input type="number" min="1" value="2">` |
| SUB | `sub_latency` | 2 | `<input type="number" min="1" value="2">` |
| MUL | `mul_latency` | 10 | `<input type="number" min="1" value="10">` |
| DIV | `div_latency` | 40 | `<input type="number" min="1" value="40">` |

- 所有值必须 ≥ 1 的整数
- 载入时随指令一起发送到后端：`POST /api/load { "code": "...", "latencies": {...} }`

#### 运行控制

| 控件 | 功能 | 快捷键 |
|------|------|--------|
| `[单步]` | 执行 1 个时钟周期 | `Space` |
| `[▶ 运行]` | 连续执行直到程序结束 | `R` |
| `[⏸ 暂停]` | 暂停连续执行 | `P` |
| `[↺ 重置]` | PC=0，清空所有状态，cycle=0 | — |
| 速度滑块 | 连续运行时周期间隔：100ms ~ 2000ms，默认 500ms | `+`/`-` |

#### 周期跳转

- 输入框输入目标周期号 + `[跳转]` 按钮
- 后端 `POST /api/history { "cycle": N }` 返回历史快照
- 跳转后进入历史回看模式，面板只读
- `[回到最新]` 按钮切回实时模式
- 历史滑块也可用（与跳转联动）

#### 状态栏

- 页面底部显示：`当前周期: N | 状态: ● 运行中 / ○ 已暂停 / ✓ 执行完毕`

---

### 6.7 事件日志

- 底部滚动文本区，固定高度约 120px
- 每条事件一行，标周期号
- 颜色按事件类型：
  - Issue → 蓝色
  - Execute 开始 → 橙色
  - Write Result / CDB → 绿色
  - Stall（结构冲突/操作数未就绪）→ 红色

---

### 6.8 前端 JS 逻辑

```javascript
let state = null;
let historyMode = false;
let speedMs = 500;
let runTimer = null;

async function refresh() {
    state = await fetch('/api/state').then(r => r.json());
    renderAll(state);
}

async function step(cycles = 1) {
    state = await fetch('/api/step', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cycles})
    }).then(r => r.json());
    historyMode = false;
    renderAll(state);
}

async function loadProgram() {
    const code = document.getElementById('code-input').value;
    const latencies = {
        load: parseInt(document.getElementById('lat-load').value),
        store: parseInt(document.getElementById('lat-store').value),
        add: parseInt(document.getElementById('lat-add').value),
        sub: parseInt(document.getElementById('lat-sub').value),
        mul: parseInt(document.getElementById('lat-mul').value),
        div: parseInt(document.getElementById('lat-div').value)
    };
    const res = await fetch('/api/load', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code, latencies})
    });
    if (!res.ok) {
        const err = await res.json();
        showError(`第${err.line}行: ${err.error}`);
        return;
    }
    await refresh();
    showSuccess(`已加载 ${state.instructions.length} 条指令`);
}

async function jumpToCycle(cycle) {
    state = await fetch('/api/history', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cycle: parseInt(cycle)})
    }).then(r => r.json());
    historyMode = true;
    renderAll(state);
}

function backToLive() {
    historyMode = false;
    refresh();
}

async function run() {
    await fetch('/api/run', { method: 'POST' });
    runTimer = setInterval(async () => {
        await refresh();
        if (!state.running) { clearInterval(runTimer); runTimer = null; }
    }, speedMs);
}

async function pause() {
    await fetch('/api/pause', { method: 'POST' });
    if (runTimer) { clearInterval(runTimer); runTimer = null; }
    await refresh();
}

function adjustSpeed(delta) {
    speedMs = Math.max(100, Math.min(2000, speedMs + delta));
    document.getElementById('speed-value').textContent = speedMs + 'ms';
    // 如果正在运行，重启定时器以应用新速度
    if (runTimer) {
        clearInterval(runTimer);
        runTimer = setInterval(async () => {
            await refresh();
            if (!state.running) { clearInterval(runTimer); runTimer = null; }
        }, speedMs);
    }
}

document.addEventListener('keydown', (e) => {
    // 在输入框中不触发快捷键
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === ' ') { e.preventDefault(); step(1); }
    if (e.key === 'r' || e.key === 'R') { run(); }
    if (e.key === 'p' || e.key === 'P') { pause(); }
    if (e.key === '+' || e.key === '=') { adjustSpeed(-100); }
    if (e.key === '-') { adjustSpeed(100); }
});
```

### 验证标准
- 所有面板加载后显示正确初始状态
- 文件输入和文本输入两种方式均能正确载入程序
- 功能部件延迟修改后，执行周期数正确反映设置值
- 点击单步，四个状态表同步更新
- 跳转到历史周期后所有面板显示对应周期状态，`[回到最新]` 切回实时
- 非法指令载入时前端正确显示错误行号和原因
- 浅色主题风格一致，无暗色/深色元素

---

## Step 7：测试用例 + 性能统计

**目标**：设计 3 种测试场景并输出性能分析

### 测试场景1：基本流水（无 WAR/WAW 冲突）

```asm
l.d   $f0, 0(R1)
l.d   $f2, 8(R1)
add.d $f4, $f0, $f2
l.d   $f6, 16(R1)
add.d $f8, $f6, $f0
```

所有指令间仅存在 RAW（真数据依赖），无 WAR/WAW 冲突，展示 Tomasulo 基本流水调度。

### 测试场景2：RAW 冲突

```asm
l.d   $f0, 0(R1)
l.d   $f2, 8(R1)
mul.d $f4, $f0, $f2      # 依赖 $f0, $f2
add.d $f6, $f4, $f2      # 依赖 $f4（RAW：等 mul.d 写完 $f4）
s.d   $f6, 16(R1)        # 依赖 $f6
```

- mul.d 需等 l.d 完成（Load-Use 依赖）
- add.d 需等 mul.d 完成写 $f4（RAW）
- 验证保留站 Qj/Qk 正确标记，CDB 广播解除等待

### 测试场景3：WAR 冲突 + RAW 混合

```asm
l.d   $f0, 0(R1)
l.d   $f2, 8(R1)
l.d   $f4, 16(R1)         # 定义 $f4 — mul.d 的源操作数
add.d $f8, $f0, $f2       # 读 $f2（此时 $f2 就绪）
mul.d $f2, $f0, $f4       # 写 $f2 — WAR：但 add.d 已取到旧值
add.d $f10, $f2, $f8      # 依赖新 $f2（RAW：等 mul.d 写完）
```

- 第一条 add.d 读 $f2 旧值 → 流出时 Vj/Vk 直接取到值
- mul.d 覆盖 $f2 寄存器状态 → WAR 通过寄存器重命名解决
- 第二条 add.d 等待新 $f2 → RAW 正常处理

### 性能统计输出

```
总执行周期数：          24
完成指令数：            5
RAW 等待周期：          12（mul.d 10 周期 + load 延迟）
WAR 停顿：              0（寄存器重命名消除）
结构冲突：              0
CPI（周期/指令）：       4.8
```

### 验证标准
- 三种场景均可正常演示
- 保留站和状态表内容与理论分析一致
- 报告中以网页截图展示执行过程的关键周期

---

## 开发顺序

```
Step 1 (数据通路)
   ↓
Step 2 (指令集)
   ↓
Step 3 (Tomasulo 三阶段核心)  ←── 至此可纯 Python 测试调度
   ↓
Step 4 (冲突处理验证)
   ↓
Step 5 (Flask API)            ←── 包裹为 Web 服务
   ↓
Step 6 (HTML 前端)            ←── 可视化仪表盘
   ↓
Step 7 (测试用例+统计)
```

- Step 1-4 是纯 Python 逻辑，可以写单元测试/命令行验证
- Step 5-6 是 Web 层，浏览器中实时调试
- 直接 `python app.py` 运行

---

## 时间估算

| 步骤 | 预估工时 | 难度 | 备注 |
|------|---------|------|------|
| Step 1 数据通路 | 2-3h | ★★ | FP寄存器+功能部件+延迟模型 |
| Step 2 指令集 | 1-2h | ★★ | 6条浮点指令解析 |
| Step 3 Tomasulo 核心 | 6-8h | ★★★★★ | Issue/Execute/Write Result 三阶段+CDB |
| Step 4 冲突处理 | 3-4h | ★★★★ | RAW/WAR/WAW 验证+event日志 |
| Step 5 Flask API | 2-3h | ★★ | 路由+JSON序列化+历史快照 |
| Step 6 HTML 前端 | 5-7h | ★★★ | 多面板布局+历史回看+CDB可视化 |
| Step 7 测试+统计 | 2-3h | ★★ | 3种测试场景+性能统计 |
| 报告撰写 | 4-5h | ★★ | 含网页截图+实验分析 |
| **合计** | **25-35h** | | Tomasulo 比五段流水线复杂度更高 |

---

## 关键设计决策

| 决策 | 选择 | 依据 |
|------|------|------|
| 前端框架 | 纯原生 HTML/CSS/JS，浅色主题 | 轻量，与五段流水线一致 |
| 页面布局 | 左侧设置 + 右侧执行（3:7 宽度比） | 参数配置与状态监控功能分离 |
| 指令输入 | 文件导入 + 文本输入双模式 | 灵活操作，兼容两种使用习惯 |
| 功能部件延迟 | 前端可配置，载入时传入后端 | 用户自行设置各部件执行周期数 |
| 历史回看 | 每周期全量快照 + 周期跳转 | 支持跳转到任意历史周期查看状态 |
| CDB 写结果 | 每周期允许多部件写 | 教学模拟器中允许多写简化实现 |
| 寄存器状态表 | 3行×17列转置布局（寄存器名/Qi/值） | 寄存器作列，Qi和值一目了然 |
| Load/Store 表 | 合并为一个表，不显示 Qj/Qk | 减少前端暴露的内部实现细节 |
| FP 寄存器 | 仅偶数编号 F0-F30，共 16 个 | 双精度，每个占 8 字节 |
| 保留站数量 | FP Add×3, FP Mul×2 | 与经典教材一致 |
| Store 写内存时机 | 地址和数据均就绪且无更早未完成 Store | 保证内存一致性 |
| 数据协议 | JSON REST API | 前后端完全解耦，可独立开发调试 |

---

## 与五段流水线模拟器的差异对比

| 维度 | 五段流水线 | Tomasulo |
|------|-----------|----------|
| 调度方式 | 静态（按程序顺序） | 动态（按数据就绪顺序） |
| 指令窗口 | 5 段各 1 条 | 多条指令同时在不同保留站执行 |
| 寄存器冲突 | Forwarding + Stall | 寄存器重命名（保留站 tag） |
| 核心可视化 | 5 段流水线图 | 保留站 + 寄存器状态表 |
| 冲突演示 | RAW（forwarding/stall） | RAW + WAR + WAW |
| 复杂度 | ★★★ | ★★★★★ |
