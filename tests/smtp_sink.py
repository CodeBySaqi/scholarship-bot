"""Minimal SMTP sink — aiosmtpd is broken on Python 3.13 (`email.policy.message_factory`),
so the delivery test needs its own 30-line server. Records every DATA payload to disk."""
import pathlib
import socket
import threading

OUT = pathlib.Path("/tmp/smtp_out")   # rebindable: `serve(port, out_dir=...)`


def _session(conn, idx, out=OUT):
    f = conn.makefile("rwb")
    f.write(b"220 sink ESMTP\r\n")
    f.flush()

    def ok(n=250, msg="OK"):
        f.write(f"{n} {msg}\r\n".encode())
        f.flush()

    body = []
    state = {}
    while True:
        line = f.readline()
        if not line:
            break
        text = line.decode("utf-8", "ignore").rstrip("\r\n")
        up = text.upper()
        if up.startswith("EHLO") or up.startswith("HELO"):
            caps = ["250-sink", "250-PIPELINING", "250-SIZE 10485760", "250 8BITMIME"]
            f.write(("\r\n".join(caps) + "\r\n").encode())
            f.flush()
        elif up.startswith("STARTTLS"):
            ok(502, "not available")  # forces the client down the plaintext path
        elif up.startswith("MAIL FROM"):
            state["from"] = text.split(":", 1)[1].strip().strip("<>")
            ok()
        elif up.startswith("RCPT TO"):
            state.setdefault("rcpt", []).append(text.split(":", 1)[1].strip().strip("<>"))
            ok()
        elif up == "DATA":
            ok(354, "send it")
            while True:
                raw = f.readline().decode("utf-8", "ignore")
                if not raw or raw.rstrip("\r\n") == ".":
                    break
                body.append(raw.lstrip("."))
            ok(250, "queued")
            record = "\n".join([f"FROM: {state.get('from','')}", f"TO: {','.join(state.get('rcpt',[]))}", "", "".join(body)])
            out.mkdir(parents=True, exist_ok=True)
            (out / f"msg{idx}.txt").write_text(record, encoding="utf-8")
            body, state = [], {}
        elif up.startswith("RSET"):
            ok()
        elif up.startswith("NOOP"):
            ok()
        elif up.startswith("QUIT"):
            f.write(b"221 Bye\r\n"); f.flush()
            break
        else:
            ok()
    f.flush()
    conn.close()


def serve(port: int, out_dir: pathlib.Path | None = None):
    global OUT
    OUT = pathlib.Path(out_dir) if out_dir else OUT
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("msg*.txt"):
        old.unlink()
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", port))
    ls.listen(8)

    def loop():
        idx = 0
        while True:
            try:
                conn, _ = ls.accept()
            except OSError:
                return
            idx += 1
            threading.Thread(target=_session, args=(conn, idx, OUT), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return ls
