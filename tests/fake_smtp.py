"""A small SMTP server for tests: AUTH PLAIN, per-recipient behaviour, and it keeps every accepted message."""
import base64
import socketserver
import threading
from email import message_from_bytes, policy


class FakeSmtpServer:
    def __init__(self, username="user@x.org", password="secret"):
        self.username, self.password = username, password
        self.messages = []  # (mail_from, [rcpts], EmailMessage)
        self.reject_data_for: set[str] = set()  # local parts whose DATA gets 554
        self.drop_on: set[str] = set()  # local parts whose RCPT makes the server hang up
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + "\r\n").encode())
                self.wfile.flush()

            def handle(self):
                self.reply("220 fake ESMTP")
                authed, mail_from, rcpts = False, None, []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode().rstrip("\r\n")
                    verb = line.split(" ", 1)[0].upper()
                    if verb == "EHLO":
                        self.wfile.write(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
                        self.wfile.flush()
                    elif verb == "HELO":
                        self.reply("250 fake")
                    elif verb == "AUTH":
                        parts = line.split(" ")
                        _, user, password = base64.b64decode(parts[2]).decode().split("\0")
                        authed = (user, password) == (fake.username, fake.password)
                        self.reply("235 ok" if authed else "535 5.7.8 authentication failed")
                    elif verb == "MAIL":
                        if not authed:
                            self.reply("530 5.7.0 authentication required")
                            continue
                        mail_from, rcpts = line.split(":", 1)[1].strip().split(" ")[0].strip("<>"), []
                        self.reply("250 ok")
                    elif verb == "RCPT":
                        rcpt = line.split(":", 1)[1].strip().split(" ")[0].strip("<>")
                        local = rcpt.split("@")[0]
                        if local in fake.drop_on:
                            return  # hang up
                        if local == "reject":
                            self.reply("550 5.1.1 user unknown")
                        elif local == "tempfail":
                            self.reply("451 4.3.0 try again later")
                        else:
                            rcpts.append(rcpt)
                            self.reply("250 ok")
                    elif verb == "DATA":
                        self.reply("354 go ahead")
                        data = bytearray()
                        while True:
                            chunk = self.rfile.readline()
                            if chunk in (b".\r\n", b".\n") or not chunk:
                                break
                            data += chunk[1:] if chunk.startswith(b"..") else chunk
                        if any(r.split("@")[0] in fake.reject_data_for for r in rcpts):
                            self.reply("554 5.7.1 message rejected as spam")
                        else:
                            fake.messages.append((mail_from, list(rcpts), message_from_bytes(bytes(data), policy=policy.default)))
                            self.reply("250 queued")
                    elif verb == "RSET":
                        mail_from, rcpts = None, []
                        self.reply("250 ok")
                    elif verb == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("502 not implemented")

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
