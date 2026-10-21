# 协作者安装与 WRF 交付验证（2026-10-21）

本次仅整合 Git 源码，不部署或重载现有服务，不重放任务、下载和历史确认。运行中的工作目录保留原样。

各仓库须按项目结构克隆为同级目录。WRF 后端依赖同级 `DB`；认证、数据服务和 AgentScope 分别按各自说明配置。`.env`、令牌、数据库、运行输出和 SSH 私钥不随源码分发。实际计算还需要管理员配置可用的 WRF 运行发布、资料、存储和认证；安装成功不等于这些外部条件可用。

主站固定 Node.js 24.14.1 / npm 11.11.0，按 README 顺序执行 `npm ci --prefix packages/wrf-ui`、`node scripts/build-wrf-ui.mjs`、`npm ci`、`npm run build`。共享组件从 Git 源码重建并校验 `wrf-ui.lock.json`，不依赖维护者的 `.artifacts` 或 `node_modules`。

智能体前端进入 `weather_agentscope/examples/web_ui/frontend` 执行 `npm ci`、`npm run build`；使用该目录的独立 npm 锁及已入库 vendor 包。

后端在独立虚拟环境安装 `requirements.txt`；路由测试另需 `pytest httpx`。智能体按 README 安装 `.[service,storage-redis]`；业务测试另需 `fakeredis[lua]`，并设置 `PYTHONPATH=src:tests:tests/agents`。测试只能使用自有临时状态和测试端口。
