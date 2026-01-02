import os
import json
import time
import requests
from datetime import datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler

# 尝试导入 Paradex SDK
try:
    from paradex_py import ParadexSubkey
    from paradex_py.environment import Environment
except ImportError:
    # 在 Vercel 环境中，依赖会通过 requirements.txt 安装
    pass

# 配置
PUSHOVER_API_KEY = "a5v3yygn78o1tb5xwme9s8k3etd77s"
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "uo11ubapaefoznebmeqcps63osfhrd")
DROP_THRESHOLD = float(os.getenv("MONITOR_DROP_THRESHOLD", "5.0"))
TIME_WINDOW_MINUTES = int(os.getenv("MONITOR_TIME_WINDOW", "30"))

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. 验证 Cron 密钥 (可选，增加安全性)
        # auth_header = self.headers.get('Authorization')
        # if auth_header != f"Bearer {os.getenv('CRON_SECRET')}":
        #     self.send_response(401)
        #     self.end_headers()
        #     return

        try:
            # 2. 初始化 Paradex 客户端
            l2_address = os.getenv("PARADEX_L2_ADDRESS")
            l2_private_key = os.getenv("PARADEX_L2_PRIVATE_KEY")
            env_name = os.getenv("PARADEX_ENV", "PROD")
            
            if not l2_address or not l2_private_key:
                raise ValueError("Missing Paradex configuration")

            from paradex_py import ParadexSubkey
            from paradex_py.environment import Environment
            
            env = Environment.PROD if env_name == "PROD" else Environment.TESTNET
            paradex = ParadexSubkey(
                env=env,
                l2_address=l2_address,
                l2_private_key=l2_private_key,
            )

            # 3. 获取当前权益
            account_summary = paradex.api_client.fetch_account_summary()
            current_equity = Decimal(str(account_summary.get("trading_equity") or account_summary.get("account_value", 0)))

            # 4. 状态管理 (使用 Vercel KV / Redis)
            # 这里为了演示，我们假设使用 Upstash Redis 或 Vercel KV
            # 如果没有配置 KV，脚本将无法计算历史跌幅，只能发送当前状态
            kv_url = os.getenv("KV_REST_API_URL")
            kv_token = os.getenv("KV_REST_API_TOKEN")
            
            history = []
            max_equity = current_equity
            
            if kv_url and kv_token:
                # 从 Redis 读取历史
                key = f"equity_history_{l2_address}"
                res = requests.get(f"{kv_url}/get/{key}", headers={"Authorization": f"Bearer {kv_token}"})
                if res.status_code == 200:
                    data = res.json().get("result")
                    if data:
                        history = json.loads(data)
                
                # 清理旧数据 (超过 30 分钟)
                now_ts = time.time()
                cutoff_ts = now_ts - (TIME_WINDOW_MINUTES * 60)
                history = [item for item in history if item['ts'] > cutoff_ts]
                
                # 计算历史最高
                if history:
                    max_equity = max(Decimal(str(item['val'])) for item in history)
                    max_equity = max(max_equity, current_equity)
                
                # 添加新数据
                history.append({"ts": now_ts, "val": float(current_equity)})
                
                # 保存回 Redis (设置 1 小时过期)
                requests.post(f"{kv_url}/set/{key}", 
                             headers={"Authorization": f"Bearer {kv_token}"},
                             data=json.dumps(history))
                requests.post(f"{kv_url}/expire/{key}/3600", 
                             headers={"Authorization": f"Bearer {kv_token}"})

            # 5. 检查报警
            drop = 0
            if max_equity > 0:
                drop = float((max_equity - current_equity) / max_equity * 100)

            alert_sent = False
            if drop >= DROP_THRESHOLD:
                # 发送 Pushover
                msg = f"⚠️ Paradex 权益报警\n跌幅: {drop:.2f}%\n当前: ${current_equity:,.2f}\n时间窗口: {TIME_WINDOW_MINUTES}min"
                requests.post("https://api.pushover.net/1/messages.json", data={
                    "token": PUSHOVER_API_KEY,
                    "user": PUSHOVER_USER_KEY,
                    "title": "Paradex Monitor",
                    "message": msg,
                    "priority": 1
                })
                alert_sent = True

            # 6. 返回结果
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                "status": "success",
                "current_equity": float(current_equity),
                "max_equity": float(max_equity),
                "drop_percentage": drop,
                "alert_sent": alert_sent,
                "history_count": len(history)
            }
            self.wfile.write(json.dumps(response).encode())

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
        return
