"""赛事交通权益联动服务入口。

* ``python3 service.py --check``       基础自检；
* ``python3 service.py --port 8000``   启动权益与调度 HTTP 服务；
* 加 ``--demo`` 预置太原四枪演示场景，``--store events.jsonl`` 持久化事件日志。
"""

import argparse

from event_transit.api import create_server

SERVICE_ID = "event-transit"
SERVICE_NAME = "赛事交通权益联动"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--demo", action="store_true", help="预置太原四枪演示数据")
    parser.add_argument("--store", default=None, help="事件日志 JSONL 持久化路径")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    server = create_server(args.port, store=args.store, load_demo=args.demo)
    print(f"{SERVICE_NAME} 已启动：http://0.0.0.0:{args.port}（演示数据：{'是' if args.demo else '否'}）")
    server.serve_forever()


if __name__ == "__main__":
    main()
