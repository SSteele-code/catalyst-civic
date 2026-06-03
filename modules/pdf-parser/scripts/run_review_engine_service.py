import sys
from http.server import ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from src.review_engine import ReviewHandshakeService, create_http_handler


def main():
    service = ReviewHandshakeService(BASE_DIR)
    server = ThreadingHTTPServer((service.host, service.port), create_http_handler(service))
    print(f"Review engine listening on http://{service.host}:{service.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
