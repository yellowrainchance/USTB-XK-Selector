# USTB-XK-Selector · 北科大选课助手

一个面向北京科技大学「本研一体化教务管理系统」的个人选课/抢课助手（PySide6 桌面程序）。
**单账号、单机、本人自用**设计：帮你盯着目标课程余量，有余量就自动提交，省去反复刷网页。

> ⚠️ **请先阅读**
> - 本项目仅供个人学习与合法选课使用，请遵守学校教务管理规定，勿用于倒课、代抢、外挂牟利等行为。
> - 程序内置请求间隔下限（3 秒）与限流退避，但仍请合理设置轮询频率，避免对教务服务器造成压力。
> - `SESSION Cookie` 等同账号凭证：**不要提交到任何公共仓库，不要截图外发**。

---

## 功能特性

- **课程搜索**：嵌入原版选课系统查询能力，学院/类别/教师/关键词筛选（全部服务端过滤），支持翻页。
- **抢课目标**：可同时添加多门课、跨选课方式，随时调整优先级、移除。
- **定时抢课（grab）**：开抢前静默等待（零请求）→ 到点自动快轮询 → **余量 > 0 自动提交** → 复核确认。
- **直接选课（direct）**：立即对每门目标「查一次 → 提交一次 → 复核」，适合手动快速抢。
- **人工确认模式**：发现余量先弹窗通知，由你点「确认提交」再发请求，防止误抢。
- **错误分类处理**：满员继续盯（有人退课可捡）、超限选/时间冲突明确提示、会话失效自动识别。
- **本地日志存档**：每次启动一个独立日志文件（`logs/xk_log_启动时刻.log`），全程留痕、可回查。
- **系统托盘常驻**：最小化到托盘 + 桌面通知，不挡你干别的。

## 工作原理（一句话版）

```
等开抢(0请求) → 快轮询(每轮只查余量 queryKxrw)
   └─ 余量 = 容量(zrl) − 已选人数(yxzrlrs)
       ├─ 余量 = 0 → 继续下一轮
       └─ 余量 > 0 → 自动发提交 addGouwuche(+addXuanke)
                      └─ 复核 queryYxkc：真的进已选列表才算成功
```

- 轮询间隔可在设置里调，**下限 3 秒**；查询异常自动退避（×2，上限 60 秒）。
- 学期、选课规则、限选门数全部**运行时从服务器发现**，不写死，兼容学校改版。

## 目录结构

```
├── xk_core.py          # 核心库：Config / XKClient(接口封装+限流) / Monitor(抢课状态机)
├── app.py              # PySide6 桌面端（会话/搜索/目标/监控/日志/设置 六页）
├── check.py            # CLI 只读自检（验证 SESSION、查某门课余量）
├── probe.py            # CLI 逆向调试工具（探查接口，开发用）
├── smoke.py            # 离屏冒烟测试
├── _build_icon.py      # 生成应用图标的构建脚本（打包 exe 用）
├── config.example.json # 配置示例（复制为 config.json 后填写）
└── requirements.txt
```

## 快速开始

需要 **Python 3.10+**（在 3.13 上开发调试）。

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. 生成并填写本地配置（SESSION 只填在你本机的 config.json，已被 .gitignore 排除）
cp config.example.json config.json

# 3. 启动桌面程序
python app.py
```

### 首次使用四步走

1. **会话管理页**：粘贴浏览器里复制出的 `SESSION Cookie`，点「测试连接」验证；
   或直接用二维码登录自动抓取。
2. **课程搜索页**：筛选找到想选的课，点该行「＋ 添加为目标」。
3. **抢课目标页**：确认目标列表、顺序（优先级）。
4. **监控运行页**：选运行模式与时间安排，点「开始」。

## 配置说明（config.json）

| 字段 | 默认 | 说明 |
|---|---|---|
| `session` | `""` | 教务系统 SESSION Cookie，登录后粘贴，**敏感** |
| `xkfsdm` | `"sztzk-b-b"` | 选课方式代码（课程序运行时以界面实际加载为准） |
| `run_mode` | `"grab"` | `grab`=定时抢课；`direct`=直接选课 |
| `targets` | `[]` | 目标列表 `[{kcdm,kxh,kcmc,xkfsdm}]`，UI 上添加 |
| `start_mode` | `"auto"` | `auto`=跟规则开抢时间；`custom`=自定义；`now`=立即 |
| `end_mode` | `"auto"` | `auto`=跟规则截止；`custom`；`none`=不限 |
| `poll_interval` | `5` | 到点后的快轮询间隔（秒，下限 3） |
| `min_interval` | `3` | 同接口最小请求间隔（秒） |
| `auto_submit` | `true` | 余量出现是否全自动提交；关掉则人工确认 |
| `submit_mode` | `"direct"` | `direct`=只调 addGouwuche；`cart`=进购物车后再选 |

> 各字段含义、运行时会话中如何变化，看程序内「设置」页的说明即可；绝大多数配置不用手改 JSON。

## 日志

- 界面日志页：实时滚动、分级过滤、关键字搜索、可导出。
- 本地存档：**每次启动独立文件** `logs/xk_log_YYYYMMDD_HHMMSS.log`（同秒启动自动加序号），
  可在日志页底部看到当前文件路径，「打开日志目录」直接进文件夹。

## 打包为 Windows 应用（可选）

```bash
python _build_icon.py    # 生成 build/app.ico（多尺寸）
pyinstaller --noconfirm --clean --onefile --windowed \
  --name USTBSelector --icon build/app.ico app.py
# 产物：dist/USTBSelector.exe（单文件，约 40MB）
```

打包版同样支持 `--onedir`（散文件目录，启动更快）。打包后 config/logs/图标目录自动落在 exe 旁。

## 免责声明

- 本项目代码仅作技术学习与个人选课辅助，不保证在任何时间可用。
- 教务系统接口随时可能调整，失效属正常现象；如遇改动，请自行跟进或提 Issue。
- 使用本项目造成的一切后果（含违反校规、账号受限）由使用者自行承担。

---

Made for learning · 本人自用项目 · 无开源许可证（保留所有权利）
