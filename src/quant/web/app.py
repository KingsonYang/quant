from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import make_server

import quant
from quant.db.init_db import DEFAULT_DB_PATH
from quant.web.render import STATIC_DIR, render_selection_page, render_strategy_page
from quant.web.service import (
    DEFAULT_SELECTION_FORM,
    DEFAULT_STRATEGY_FORM,
    ServiceResult,
    run_selection_service,
    run_strategy_service,
    service_result_to_dict,
)


class WebApplication:
    def __init__(self, default_db_path: Path = DEFAULT_DB_PATH) -> None:
        self.default_db_path = Path(default_db_path)
        self._log(
            "startup",
            default_db_path=str(self.default_db_path.resolve()),
            cwd=os.getcwd(),
            python_executable=sys.executable,
            quant_module=str(Path(quant.__file__).resolve()),
        )

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        if path.startswith("/static/"):
            return self._serve_static(path, start_response)
        if path == "/":
            start_response("302 Found", [("Location", "/backtests/selection")])
            return [b""]
        if path == "/backtests/selection":
            return self._selection_page(method, environ, start_response)
        if path == "/backtests/strategy":
            return self._strategy_page(method, environ, start_response)
        if path == "/api/backtests/selection":
            return self._selection_api(method, environ, start_response)
        if path == "/api/backtests/strategy":
            return self._strategy_api(method, environ, start_response)

        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return ["Not Found".encode("utf-8")]

    def _log(self, event: str, **fields: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        details = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in fields.items()
        )
        print(f"[quant-web] {timestamp} event={event} {details}".rstrip())

    def _summarize_form_data(self, form_data: dict[str, str]) -> dict[str, str]:
        summary: dict[str, str] = {}
        for key, value in form_data.items():
            summary[key] = f"<len={len(value)}>" if key == "rules_json" else value
        return summary

    def _quote_command_arg(self, value: str) -> str:
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'

    def _build_cli_command(self, endpoint: str, form_data: dict[str, str]) -> str:
        command = "backtest-selection" if "selection" in endpoint else "backtest-strategy"
        option_order: list[tuple[str, str]] = [
            ("start_date", "--start-date"),
            ("end_date", "--end-date"),
            ("holding_period", "--holding-period"),
            ("max_positions", "--max-positions"),
            ("max_daily_buys", "--max-daily-buys"),
            ("initial_capital", "--initial-capital"),
            ("position_size_pct", "--position-size-pct"),
            ("take_profit_pct", "--take-profit-pct"),
            ("stop_loss_pct", "--stop-loss-pct"),
            ("logic", "--logic"),
            ("selection_sort_by", "--selection-sort-by"),
            ("selection_sort_order", "--selection-sort-order"),
            ("selection_top_n", "--selection-top-n"),
            ("db_path", "--db-path"),
        ]
        parts = ["quant", command]
        for field_name, option_name in option_order:
            value = form_data.get(field_name, "")
            if value == "":
                continue
            parts.extend([option_name, self._quote_command_arg(value)])

        rules_file = form_data.get("rules_file", "")
        rules_json = form_data.get("rules_json", "")
        if rules_file:
            parts.extend(["--rules-file", self._quote_command_arg(rules_file)])
        elif rules_json:
            parts.extend(["--rules-json", self._quote_command_arg(rules_json)])
        return " ".join(parts)

    def _log_service_result(
        self,
        endpoint: str,
        method: str,
        form_data: dict[str, str],
        result: ServiceResult,
    ) -> None:
        self._log(
            "request",
            endpoint=endpoint,
            method=method,
            ok=str(result.ok),
            field_error_count=str(len(result.field_errors)),
            global_error_count=str(len(result.global_errors)),
            form_data=json.dumps(
                self._summarize_form_data(form_data),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        self._log(
            "command",
            endpoint=endpoint,
            command=self._build_cli_command(endpoint, form_data),
        )
        if not result.ok:
            self._log(
                "request_failed",
                endpoint=endpoint,
                method=method,
                field_errors=json.dumps(result.field_errors, ensure_ascii=False, sort_keys=True),
                global_errors=json.dumps(result.global_errors, ensure_ascii=False),
            )
            if result.debug_details:
                self._log(
                    "request_exception",
                    endpoint=endpoint,
                    exception_type=str(result.debug_details.get("exception_type", "")),
                    exception_message=str(result.debug_details.get("exception_message", "")),
                    traceback=str(result.debug_details.get("traceback", "")),
                )

    def _read_form_data(self, environ) -> dict[str, str]:
        if environ.get("REQUEST_METHOD", "GET").upper() == "GET":
            raw_params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        else:
            content_length = int(environ.get("CONTENT_LENGTH") or "0")
            raw_body = environ["wsgi.input"].read(content_length).decode("utf-8")
            raw_params = parse_qs(raw_body, keep_blank_values=True)
        return {key: values[-1] for key, values in raw_params.items()}

    def _json_response(self, start_response, payload: dict, status: str = "200 OK"):
        start_response(status, [("Content-Type", "application/json; charset=utf-8")])
        return [json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")]

    def _html_response(self, start_response, html: str, status: str = "200 OK"):
        start_response(status, [("Content-Type", "text/html; charset=utf-8")])
        return [html.encode("utf-8")]

    def _empty_selection_result(self) -> ServiceResult:
        return ServiceResult(False, DEFAULT_SELECTION_FORM.copy(), {}, [], None)

    def _empty_strategy_result(self) -> ServiceResult:
        return ServiceResult(False, DEFAULT_STRATEGY_FORM.copy(), {}, [], None)

    def _selection_page(self, method, environ, start_response):
        result = self._empty_selection_result()
        if method == "POST":
            form_data = self._read_form_data(environ)
            result = run_selection_service(form_data, self.default_db_path)
            self._log_service_result("/backtests/selection", method, form_data, result)
        return self._html_response(start_response, render_selection_page(result))

    def _strategy_page(self, method, environ, start_response):
        result = self._empty_strategy_result()
        if method == "POST":
            form_data = self._read_form_data(environ)
            result = run_strategy_service(form_data, self.default_db_path)
            self._log_service_result("/backtests/strategy", method, form_data, result)
        return self._html_response(start_response, render_strategy_page(result))

    def _selection_api(self, method, environ, start_response):
        if method != "POST":
            return self._json_response(
                start_response,
                {"ok": False, "message": "Only POST is supported."},
                "405 Method Not Allowed",
            )
        form_data = self._read_form_data(environ)
        result = run_selection_service(form_data, self.default_db_path)
        self._log_service_result("/api/backtests/selection", method, form_data, result)
        status = "200 OK" if result.ok else "400 Bad Request"
        return self._json_response(start_response, service_result_to_dict(result), status)

    def _strategy_api(self, method, environ, start_response):
        if method != "POST":
            return self._json_response(
                start_response,
                {"ok": False, "message": "Only POST is supported."},
                "405 Method Not Allowed",
            )
        form_data = self._read_form_data(environ)
        result = run_strategy_service(form_data, self.default_db_path)
        self._log_service_result("/api/backtests/strategy", method, form_data, result)
        status = "200 OK" if result.ok else "400 Bad Request"
        return self._json_response(start_response, service_result_to_dict(result), status)

    def _serve_static(self, path: str, start_response):
        file_path = (STATIC_DIR / path.removeprefix("/static/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Not Found".encode("utf-8")]

        if file_path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif file_path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        else:
            content_type = "text/plain; charset=utf-8"

        start_response("200 OK", [("Content-Type", content_type)])
        return [file_path.read_bytes()]


def create_app(default_db_path: Path = DEFAULT_DB_PATH) -> WebApplication:
    return WebApplication(default_db_path=default_db_path)


def serve(host: str = "127.0.0.1", port: int = 8000, default_db_path: Path = DEFAULT_DB_PATH) -> None:
    application = create_app(default_db_path=default_db_path)
    with make_server(host, port, application) as server:
        application._log(
            "server_ready",
            host=host,
            port=str(port),
            url=f"http://{host}:{port}",
        )
        server.serve_forever()
