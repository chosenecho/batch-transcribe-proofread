"""Synthetic loopback S3 wire endpoint; not a claim of real R2 acceptance."""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET


@contextmanager
def s3_endpoint():
    state = SimpleNamespace(objects={}, uploads={}, calls=[], fail_part=None, deny=False, corrupt=False, short=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def response(self, status=200, payload=b"", **headers):
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def error(self, code, status=400):
            self.response(status, f"<Error><Code>{code}</Code></Error>".encode(), Content_Type="application/xml")

        def operation(self):
            parsed = urlsplit(self.path)
            key, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
            state.calls.append((self.command, key, tuple(sorted(query))))
            if state.deny or not self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256 "):
                self.error("AccessDenied", 403)
                return
            if self.command == "HEAD":
                self.response()
            elif self.command == "GET" and "uploadId" in query:
                upload = state.uploads.get(query["uploadId"][0])
                if upload is None:
                    self.error("NoSuchUpload", 404)
                    return
                parts = "".join(f"<Part><PartNumber>{i}</PartNumber><ETag>{etag}</ETag><Size>{len(data)}</Size></Part>"
                                for i, (data, etag) in sorted(upload.items()))
                self.response(payload=f"<ListPartsResult><IsTruncated>false</IsTruncated>{parts}</ListPartsResult>".encode())
            elif self.command == "GET":
                data = state.objects.get(key)
                if data is None:
                    self.error("NoSuchKey", 404)
                    return
                if state.corrupt:
                    data = bytes([data[0] ^ 1]) + data[1:] if data else b"corrupt"
                headers, status = {}, 200
                if "Range" in self.headers:
                    start, end = map(int, self.headers["Range"].removeprefix("bytes=").split("-"))
                    headers["Content_Range"] = f"bytes {start}-{end}/{len(data)}"
                    data, status = data[start:end + 1], 206
                if state.short:
                    data = data[:-1]
                self.response(status, data, **headers)
            elif self.command == "PUT":
                data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if "uploadId" in query:
                    number = int(query["partNumber"][0])
                    if number == state.fail_part:
                        self.error("AccessDenied", 403)
                        return
                    upload = state.uploads.get(query["uploadId"][0])
                    if upload is None:
                        self.error("NoSuchUpload", 404)
                        return
                    etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
                    upload[number] = data, etag
                    self.response(ETag=etag)
                else:
                    state.objects[key] = data
                    self.response()
            elif self.command == "POST" and "uploads" in query:
                identity = f"upload-{len(state.uploads) + 1}"
                state.uploads[identity] = {}
                self.response(payload=f"<InitiateMultipartUploadResult><UploadId>{identity}</UploadId></InitiateMultipartUploadResult>".encode())
            elif self.command == "POST":
                identity = query["uploadId"][0]
                upload = state.uploads.get(identity)
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if upload is None:
                    self.error("NoSuchUpload", 404)
                    return
                root = ET.fromstring(raw)
                numbers = [int(element.text) for element in root.iter() if element.tag.endswith("PartNumber")]
                state.objects[key] = b"".join(upload[i][0] for i in numbers)
                del state.uploads[identity]
                self.response(payload=b'<CompleteMultipartUploadResult><ETag>"opaque-multipart-etag"</ETag></CompleteMultipartUploadResult>')
            else:
                state.uploads.pop(query["uploadId"][0], None)
                self.response(204)

        do_HEAD = do_GET = do_POST = do_PUT = do_DELETE = operation

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
