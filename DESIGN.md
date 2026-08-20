---
name: MiniWorld
description: 温暖、可信、适合长期观察的社会模拟研究台
colors:
  forest-green: "#245847"
  forest-ink: "#16231f"
  signal-lime: "#c7e36e"
  signal-blue: "#2f6f8f"
  warning-amber: "#c37a24"
  destructive-red: "#b4332d"
  warm-paper: "#f4f1e9"
  card-paper: "#fffdf7"
  muted-graphite: "#708079"
  hairline: "#d9d7cc"
  info-surface: "#e7f0f4"
  warning-surface: "#fff1e6"
typography:
  display:
    fontFamily: "Georgia, serif"
    fontSize: "clamp(2rem, 4vw, 3.375rem)"
    fontWeight: 800
    lineHeight: 0.95
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "Georgia, serif"
    fontSize: "1.625rem"
    fontWeight: 700
    lineHeight: 1.1
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "0.08em"
rounded:
  sm: "10px"
  md: "12px"
  lg: "18px"
  pill: "999px"
  circle: "50%"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  2xl: "38px"
components:
  button-primary:
    backgroundColor: "{colors.forest-green}"
    textColor: "{colors.card-paper}"
    rounded: "{rounded.pill}"
    padding: "8px 13px"
  button-information:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.card-paper}"
    rounded: "{rounded.pill}"
    padding: "8px 13px"
  button-danger:
    backgroundColor: "{colors.destructive-red}"
    textColor: "{colors.card-paper}"
    rounded: "{rounded.pill}"
    padding: "8px 13px"
  card-observation:
    backgroundColor: "{colors.card-paper}"
    textColor: "{colors.forest-ink}"
    rounded: "{rounded.lg}"
    padding: "24px"
  tab-active:
    backgroundColor: "{colors.info-surface}"
    textColor: "{colors.signal-blue}"
    rounded: "{rounded.pill}"
    padding: "8px 12px"
---

# Design System: MiniWorld

## Overview

**Creative North Star: "活体世界观测站"**

MiniWorld 是一张温暖、可信、适合长时间停留的社会模拟研究台。它借用自然观察站、研究笔记与纸质档案的气质：界面有明确的层级和事实边界，但不会像军事控制室那样冷峻，也不会像游戏 HUD 那样把每一项数据都变成高声量装饰。

视觉系统保留现有森林绿、米白纸面、石墨文字和衬线标题。状态色只在需要判断和行动时出现：信号蓝表示信息与选中，琥珀表示警告，红色只表示急停或破坏性操作；青柠保留为生命感、运行正常和关键品牌强调。大量信息通过稳定网格、轻分层纸面和渐进披露组织，而不是通过更多色块制造层级。

**Key Characteristics:**

- 温暖纸张底色上的高密度、可长时间阅读的信息。
- 衬线标题提供“观察档案”感，无衬线正文保证操作清晰。
- 状态色稀缺且语义固定，不承担装饰任务。
- 默认平静，只有异常、选择和危险操作提高视觉声量。
- 首页用于扫读与判断，详情用于解释与审计。

## Colors

调色板以森林与纸张为基底，以少量信号色表达状态，不把业务模块各自染成独立主题。

### Primary

- **森林绿：** 产品标识、主要操作、关键标题背景和运行稳定的可信基调。
- **森林墨色：** 主文字、深色标题和高对比信息，不以纯黑压低纸面温度。
- **生命青柠：** 品牌强调、健康运行和少量正向反馈；在单屏中保持稀缺。

### Secondary

- **信号蓝：** 信息提示、当前选中、标签导航激活态和可继续探索的交互。

### Tertiary

- **琥珀警示：** 预算逼近、队列堆积、fallback 增多和需要关注但尚未危险的状态。
- **破坏性红：** 仅用于紧急停止、重置和不可逆或高风险确认。

### Neutral

- **温暖纸张：** 页面底色，承载长期观察的低眩光基底。
- **卡片纸张：** 内容表面，与页面通过轻微明度差和细边线分层。
- **弱化石墨：** 次要说明、元数据和时间标签。
- **发丝边线：** 卡片、列表与内部区域的安静分隔。

**The Signal Discipline Rule.** 蓝色只表示信息或选中，琥珀只表示警告，红色只表示破坏性动作；同一语义不得在不同模块换色。

**The Quiet Field Rule.** 大面积表面使用纸张和中性色，业务类别依靠标题、分组和标签区分，不依靠彩虹色块区分。

## Typography

**Display Font:** Georgia（衬线回退）  
**Body Font:** Inter 与系统无衬线字体栈  
**Label/Mono Font:** 系统等宽字体栈

**Character:** 衬线标题像观察档案与研究章节，无衬线正文负责清晰说明，等宽标签只标记时间、版本、队列和审计元数据。三种声音分工明确，不把小字号等宽文字当成主要内容字体。

### Hierarchy

- **Display：** 粗衬线、紧凑行高，用于产品名称与页面级识别。
- **Headline：** 粗衬线，用于一级区域和观察档案标题。
- **Body：** 常规无衬线，承担说明、事件描述与解释文本；长段落保持舒展行高。
- **Label：** 等宽或高辨识度无衬线，承担版本、状态、时间和量化元数据；实际界面不得低于 12px。

**The Readability Before Density Rule.** 信息密度通过分组、折叠和列布局获得，不通过 9–10px 的正文或元数据压缩获得。

## Layout

桌面端使用居中宽容器与稳定的十二列思维：首页依次呈现安全与运行状态、世界总览、五位 NPC、世界脉搏，再进入按需展开的趋势和审计。顶部导航与关键控制保持可达，但破坏性操作与普通浏览动作在空间上分离。

观察卡片以 12–24px 的内部节奏组织内容，区域之间使用 24–38px 的呼吸。高频状态优先形成横向扫读，解释文本和时间线形成纵向阅读。详情不再使用单条超长抽屉，而是使用有固定身份区和标签导航的观察档案。

响应式按内容压力而非设备名称处理：宽桌面保持多列；平板压缩到两列并允许标签导航滚动；窄屏改为单列、全屏 NPC 档案和粘性档案导航。任何断点都不能造成页面级横向滚动，控制的最小触控尺寸为 44×44px。

## Elevation & Depth

系统采用“纸张轻分层”而非玻璃或强悬浮。大多数卡片依靠纸张明度差、细边线和圆角建立结构；阴影只用于需要从文档流中明确抬起的观察卡、弹出反馈或悬停状态。模糊透明背景不属于这个世界。

### Shadow Vocabulary

- **静态观察层：** 柔和、低不透明度的森林色环境阴影，用于少量主要卡片。
- **交互抬升层：** NPC 卡片悬停时轻微上移并增加环境阴影，表示可进入观察档案。
- **状态光环：** 小范围外圈只用于在线、暂停等状态点，不扩散到整张卡片。

**The Paper Stack Rule.** 默认表面是平的纸张层；阴影必须解释层级或交互，不作为装饰纹理。

## Shapes

主要容器使用温和的大圆角，内部卡片使用中等圆角，状态与筛选标签使用胶囊形。圆形只用于头像、状态点和关闭类图标按钮。整个系统避免锐利军用面板、六边形 HUD、发光描边和过度切角。

圆角形成三层：10px 用于细节与小容器，12px 用于可交互卡片，18px 用于区域级纸张表面；胶囊形仅用于短标签和单行动作，不把长文本或所有容器都做成药丸。

## Components

### Buttons

- **Shape:** 安静且易触达的圆角或胶囊按钮；移动端保证 44px 高度。
- **Primary:** 森林绿底、米白文字，用于普通安全操作。
- **Information / Selected:** 信号蓝或浅蓝纸面，用于当前标签和信息动作。
- **Danger:** 破坏性红，只用于急停、重置和相同风险等级的确认。
- **Hover / Focus:** 悬停仅提高明度或轻微抬升；键盘焦点使用清晰的蓝色外圈，不仅依赖颜色变化。

### Chips

- **Style:** 纸面或轻色底、短文本、明确图标或文字状态。
- **State:** 选中使用信号蓝；警告使用琥珀；正常状态可使用森林绿或生命青柠，但必须同时提供文字。

### Cards / Containers

- **Corner Style:** 区域表面 18px，可交互卡片 12px。
- **Background:** 温暖纸张上的卡片纸张，状态表面只做轻微染色。
- **Shadow Strategy:** 默认边线分层；重要观察卡和悬停态才出现环境阴影。
- **Internal Padding:** 高频密集卡 12–16px，区域卡 24px。

### Navigation

- 页面导航使用粘性纸张栏，文本清晰、数量克制；激活项以信号蓝文字、底色和可见标记共同表达。
- NPC 观察档案使用标签导航切换数据域，支持键盘方向键、深链接和窄屏横向滚动。
- 导航不使用游戏式雷达、发光图标或大面积深色 HUD。

### NPC Observation Card

NPC 卡片是首页的核心观察单元：姓名、职业、地点与当前行为形成第一层；能量、饥饿、心情和社交需求形成第二层；Agent 状态、fallback 或异常形成第三层。整张卡可进入观察档案，但内部状态不得依赖悬停才能看到。

### World Pulse Timeline

时间线将关键事件、待处理承诺、会话、fallback 与人生变化合并为可筛选的世界脉搏。时间、事件类型、人物与解释保持固定阅读顺序；重要性通过标记和排版体现，不通过为每种事件建立新颜色。

## Do's and Don'ts

### Do:

- **Do** 让运行安全、异常和危险操作在任何滚动位置都容易找到。
- **Do** 用稳定分组、折叠和标签导航承载全部既有信息。
- **Do** 同时用文字、图标或形状表达状态，不让颜色成为唯一信号。
- **Do** 让动态刷新保留焦点、滚动位置和用户正在阅读的内容。
- **Do** 把 Engine 事实、Agent 建议、最终行动和 fallback 原因视觉分层。

### Don't:

- **Don't** 使用玻璃拟态、霓虹辉光、军事控制室、电竞 HUD 或过度装饰的数据墙。
- **Don't** 用不同粉彩背景替代清晰的信息架构。
- **Don't** 在首页默认展开所有经济、职业、社区、社交和故事细节。
- **Don't** 把红色用于普通失败、一般警告或装饰性强调。
- **Don't** 用超长抽屉承载完整 NPC 档案，也不要在打开档案时一次请求全部低频数据。
