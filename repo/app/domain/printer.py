from __future__ import annotations

import socket
from dataclasses import dataclass

from app.infra.config import Settings


@dataclass
class ReceiptPayload:
    order_id: str
    lines: list[str]
    total: float

    def to_escpos(self) -> bytes:
        body = "\n".join(self.lines)
        text = f"ORDER: {self.order_id}\n{body}\nTOTAL: {self.total:.2f}\n\n"
        return text.encode("utf-8")


class ReceiptPrinterAdapter:
    def print_receipt(self, payload: ReceiptPayload) -> None:
        raise NotImplementedError


class NoopPrinter(ReceiptPrinterAdapter):
    def print_receipt(self, payload: ReceiptPayload) -> None:
        _ = payload


class EscPosNetworkPrinter(ReceiptPrinterAdapter):
    def __init__(self, host: str, port: int = 9100, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def print_receipt(self, payload: ReceiptPayload) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(payload.to_escpos())


class LocalDevicePrinter(ReceiptPrinterAdapter):
    def __init__(self, device_path: str = "/dev/usb/lp0") -> None:
        self.device_path = device_path

    def print_receipt(self, payload: ReceiptPayload) -> None:
        with open(self.device_path, "wb") as fh:
            fh.write(payload.to_escpos())


def build_printer_adapter(settings: Settings) -> ReceiptPrinterAdapter:
    backend = settings.receipt_printer_backend.lower()
    if backend == "network":
        return EscPosNetworkPrinter(host=settings.receipt_printer_host, port=settings.receipt_printer_port)
    if backend == "device":
        return LocalDevicePrinter(device_path=settings.receipt_printer_device_path)
    return NoopPrinter()
