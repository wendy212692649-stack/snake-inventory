# 云端可编辑工作台 · 部署说明

## 架构
- `server.py`：Flask 服务，提供页面 + 读写飞书 OpenAPI 接口
- `cloud_sync.py`：文件1(总台账+租借) ⇄ 文件2(主播看板) 双向同步逻辑
- `feishu_api.py`：飞书 OpenAPI 客户端（应用凭证）
- 页面（`build_pages.py` 生成）优先 `fetch('/api/data')` 实时拉取，失败回退内嵌快照

## 环境变量（必填）
```
FEISHU_APP_ID=<你的 App ID>
FEISHU_APP_SECRET=<你的 App Secret>
PORT=8000   # 可选，默认 8000
```

## 本地启动（验证用）
```bash
pip install -r requirements.txt
export FEISHU_APP_ID=<你的 App ID> FEISHU_APP_SECRET=<你的 App Secret>
python server.py
# 总工作台 http://localhost:8000/   主播工作台 http://localhost:8000/anchor
```

## 云端部署（推荐 Railway / Render，免费 Python 托管）
1. 将 `cloud/` 目录作为项目根（含 server.py / cloud_sync.py / feishu_api.py / requirements.txt）
2. 启动命令：`pip install -r requirements.txt && python server.py`
3. 在平台后台配置上面两个环境变量
4. 部署后得到公开 URL，任意设备打开即可编辑 + 实时同步飞书

## 已验证（本地 HTTP 端到端 2026-08-17）
- GET /api/data → 10 总台账 / 3 租借 / 7 主播在售 ✅
- 网页新增在售 → 文件1 创建 + 自动同步文件2 ✅
- 标记已售 → 文件1 处置=已售+出库时间 + 文件2 移除 ✅
- 删除 → 双表干净 ✅
- 日期毫秒时间戳、图片带 token 下载均正确 ✅
