"""OpenTelemetry logs/metrics/traces setup.

Emits JSON logs to stdout (suitable for scraping by a log collector such as
Grafana Alloy) plus OTLP gRPC exporters when OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""

import logging
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from sprintbaton.dependencies import require_storage_module
from sprintbaton.config.settings import Settings
from sprintbaton.observer import console
from sprintbaton.observer import context as log_ctx
from sprintbaton.observer import verbosity as verbosity_mod

_ENVIRONMENT = ""
_SERVICE = "sprintbaton"
# Which entrypoint within the deployment emitted the line (cli-logging spec
# §4.3): "worker" (serve) | "api" (serve-api) | "cli" (everything else).
# Additive to OTEL_SERVICE_NAME, which still names the deployment.
_PROCESS_ROLE = "cli"


def set_process_role(role: str) -> None:
    global _PROCESS_ROLE
    _PROCESS_ROLE = role


class OtelJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        log_record["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"
        log_record["level"] = record.levelname.lower()
        log_record["message"] = record.getMessage()
        log_record["service"] = _SERVICE
        log_record["environment"] = _ENVIRONMENT
        log_record["process_role"] = _PROCESS_ROLE

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            log_record["trace_id"] = format(ctx.trace_id, "032x")
            log_record["span_id"] = format(ctx.span_id, "016x")
        else:
            log_record["trace_id"] = ""
            log_record["span_id"] = ""

        # Request-scoped identifiers (cli-logging spec §4.2): empty fields are
        # dropped rather than emitted as "" (most lines run outside any task's
        # process() call), and an explicit extra={...} value wins over the
        # ambient context.
        for key, value in log_ctx.current().items():
            if value and not log_record.get(key):
                log_record[key] = value

        for key in ("asctime", "color_message", "taskName"):
            log_record.pop(key, None)


def setup_logging(settings: Settings, process_role: str | None = None) -> None:
    global _ENVIRONMENT, _SERVICE, _PROCESS_ROLE
    if process_role is not None:
        _PROCESS_ROLE = process_role
    _SERVICE = settings.otel_service_name
    for part in settings.otel_resource_attributes.split(","):
        if part.startswith("deployment.environment="):
            _ENVIRONMENT = part.split("=", 1)[1]
            break

    root = logging.getLogger()
    # The console renderer, when installed (tool mode + TTY, cli-logging spec
    # §8), replaces the JSON handler — never both at once, to avoid
    # interleaving two formats on one terminal.
    if not console.is_installed():
        handler = logging.StreamHandler()
        handler.setFormatter(OtelJsonFormatter())
        root.handlers.clear()
        root.addHandler(handler)
    # Verbosity's level floor wins at silent/verbose; NORMAL keeps LOG_LEVEL
    # authoritative, exactly today's behavior (cli-logging spec §3).
    root.setLevel(verbosity_mod.effective_root_level(settings.log_level))


def setup_telemetry(settings: Settings) -> None:
    endpoint = settings.otel_exporter_otlp_endpoint

    resource = Resource.create({
        SERVICE_NAME: settings.otel_service_name,
        "deployment.environment": _ENVIRONMENT,
    })

    tracer_provider = TracerProvider(resource=resource)
    meter_readers = []
    if endpoint:
        # Lazy import: the OTLP gRPC exporter is the `otlp` extra (it pulls
        # in grpcio) — a tool-mode install has no OTLP collector to export to,
        # so this branch, and the import, only fire when an endpoint is set.
        require_storage_module(
            "opentelemetry.exporter.otlp.proto.grpc", extra="otlp",
            package="opentelemetry-exporter-otlp-proto-grpc",
            feature="OTLP export (OTEL_EXPORTER_OTLP_ENDPOINT)")
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        meter_readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))
        )

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
        )
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=meter_readers))

    # Datastore instrumentation is activated by the backend that is actually
    # selected, not here (pluggable-hosted-backends spec §4.6): MongoStorage
    # instruments pymongo when it is constructed.
