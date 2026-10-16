# WRF 与系统产品显示核对（2026-10-09）

本核对针对源码中的单位、色表和阈值合同；不修改历史产品、系统主题或底图。

| 项目 | WRF 工作台 | 系统 GFS / ERA5 | 核对结论 |
| --- | --- | --- | --- |
| 温度 | 原始 K；显示 °C = K − 273.15 | GFS K 转 °C；ERA5 元数据保留原单位 | 物理等价，必须随产品显示单位 |
| 气压 | 原始 Pa；显示 hPa = Pa / 100 | GFS 同样转换；ERA5 元数据决定 | 不把海平面气压与地面气压混称 |
| 风 | U10/V10 显示 m/s，允许负值 | GFS 分量 m/s；ERA5 另有非负风速产品 | 分量与风速不能共享阈值解释 |
| 降水 | RAINC / RAINNC，起报以来累计 mm | GFS/ERA5 的累积窗由输入产品定义 | 非相同累积窗不能直接比较；不是降水率 |
| 显示精度 | 常见变量 1 位小数，高度整数；仅显示舍入 | GFS 统计常见 3 位小数 | 精度不改原始数据或产品 digest |
| 色表 | 按发布元数据 RGB；不同变量独立色表 | GFS turbo/viridis；ERA5 另有五色色表 | 现有色表不同，不能宣称同颜色代表同数值 |
| 色域 | WRF 2–98 百分位，退化时 min/max | GFS 温度 p05–p95、气压均值±18 hPa、降水动态上限；ERA5 按元数据 | 必须读取各自色标，不把裁剪范围称作全场极值 |

CF 规定变量单位必须与标准单位物理等价，明确允许摄氏度与开尔文转换；参见 [CF 标准名表的单位说明](https://cfconventions.org/Data/cf-standard-names/85/build/cf-standard-name-table.html)。CF 的单位规范不证明项目自定义 RGB 表是国际统一色表。[NCAR 官方色表说明](https://www.ncl.ucar.edu/Document/Graphics/color_tables.shtml)提供多种色表，也不能据此给当前自定义色表冠以统一标准。

源码依据：`backend_wrf/renderer.py`、共享包 `src/presentation.js` 与 `WrfResults.vue`、`backend_system/adapters/gfs_adapter.py` 和 `era5_adapter.py`。工作台转换的是数值标签，保持既有像素与色表一致；遇到无合法色表的旧产品不伪造渐变。

验收结论：单位转换及数量语义可一致解释；历史色表和阈值保留且明确存在差异。本轮不宣称跨产品颜色已统一，亦不为追求一致重渲染历史产品。若以后增加统一比较模式，应创建带版本的新产品，固定相同变量、单位、时间窗和色域，再共享色表；不能仅替换图例。
