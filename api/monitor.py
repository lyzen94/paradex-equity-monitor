import os
import json
import time
import requests
import traceback
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler

# 配置
PUSHOVER_API_KEY = "a5v3yygn78o1tb5xwme9s8k3etd77s"
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "uo11ubapaefoznebmeqcps63osfhrd")
DROP_THRESHOLD = float(os.getenv("MONITOR_DROP_THRESHOLD", "5.0"))
TIME_WINDOW_MINUTES = int(os.getenv("MONITOR_TIME_WINDOW", "30"))

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            # 1. 获取环境变量
            l2_address = os.getenv("PARADEX_L2_ADDRESS")
            l2_private_key = os.getenv("PARADEX_L2_PRIVATE_KEY")
            env_name = os.getenv("PARADEX_ENV", "PROD")
            
            if not l2_address or not l2_private_key:
                raise ValueError("Missing PARADEX_L2_ADDRESS or PARADEX_L2_PRIVATE_KEY environment variables")

            # 2. 延迟导入 SDK 以捕获可能的导入错误
            try:
                from paradex_py import ParadexSubkey
                from paradex_py.environment import Environment, PROD, TESTNET
            except ImportError as e:
                raise ImportError(f"Failed to import paradex-py: {str(e)}")

            # 3. 初始化 Paradex 客户端
            # 显式指定环境，避免 SDK 内部查找失败
            target_env = PROD if env_name == "PROD" else TESTNET
            
            try:
                paradex = ParadexSubkey(
                    env=target_env,
                    l2_address=l2_address,
                    l2_private_key=l2_private_key,
                )
            except Exception as e:
                raise Exception(f"Failed to initialize ParadexSubkey: {str(e)}")

            # 4. 获取当前权益
            try:
                account_summary = paradex.api_client.fetch_account_summary()
                # 尝试多个可能的字段名
                equity_val = account_summary.get("trading_equity") or \
                             account_summary.get("account_value") or \
                             account_summary.get("total_equity") or 0
                current_equity = Decimal(str(equity_val))
            except Exception as e:
                raise Exception(f"Failed to fetch account summary: {str(e)}")

            # 5. 状态管理 (Vercel KV / Redis)
            kv_url = os.getenv("KV_REST_API_URL")
            kv_token = os.getenv("KV_REST_API_TOKEN")
            
            history = []
            max_equity = current_equity
            
            if kv_url and kv_token:
                key = f"equity_history_{l2_address}"
                # 确保 URL 格式正确
                base_url = kv_url.rstrip('/')
                
                # 读取历史
                try:
                    res = requests.get(f"{base_url}/get/{key}", headers={"Authorization": f"Bearer {kv_token}"}, timeout=5)
                    if res.status_code == 200:
                        data = res.json().get("result")
                        if data:
                            history = json.loads(data)
                except:
                    pass # KV 读取失败不应中断监控
                
                # 清理旧数据
                now_ts = time.time()
                cutoff_ts = now_ts - (TIME_WINDOW_MINUTES * 60)
                history = [item for item in history if item['ts'] > cutoff_ts]
                
                # 计算历史最高
                if history:
                    max_equity = max(Decimal(str(item['val'])) for item in history)
                    max_equity = max(max_equity, current_equity)
                
                # 添加新数据
                history.append({"ts": now_ts, "val": float(current_equity)})
                
                # 保存回 Redis
                try:
                    requests.post(f"{base_url}/set/{key}", 
                                 headers={"Authorization": f"Bearer {kv_token}"},
                                 data=json.dumps(history),
                                 timeout=5)
                    requests.post(f"{base_url}/expire/{key}/3600", 
                                 headers={"Authorization": f"Bearer {kv_token}"},
                                 timeout=5)
                except:
                    pass

            # 6. 检查报警
            drop = 0
            if max_equity > 0:
                drop = float((max_equity - current_equity) / max_equity * 100)

            alert_sent = False
            if drop >= DROP_THRESHOLD:
                msg = f"⚠️ Paradex 权益报警\n跌幅: {drop:.2f}%\n当前: ${current_equity:,.2f}\n时间窗口: {TIME_WINDOW_MINUTES}min"
                try:
                    requests.post("https://api.pushover.net/1/messages.json", data={
                        "token": PUSHOVER_API_KEY,
                        "user": PUSHOVER_USER_KEY,
                        "title": "Paradex Monitor",
                        "message": msg,
                        "priority": 1
                    }, timeout=10)
                    alert_sent = True
                except:
                    pass

            # 7. 返回成功响应
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "current_equity": float(current_equity),
                "max_equity_in_window": float(max_equity),
                "drop_percentage": round(drop, 4),
                "alert_sent": alert_sent,
                "kv_enabled": bool(kv_url)
            }
            self.wfile.write(json.dumps(response).encode())

        except Exception as e:
            # 返回详细错误信息以便调试
            self.send_response(200) # 即使出错也返回 200，方便在浏览器看 JSON
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            error_response = {
                "status": "error",
                "message": str(e),
                "traceback": traceback.format_exc() if os.getenv("DEBUG") else "Set DEBUG=1 for full trace"
            }
            self.wfile.write(json.dumps(error_response).encode())
        return
