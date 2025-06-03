# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import os
import threading
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from aiopslab.observer.trace_api import TraceAPI
from aiopslab.observer.log_api import LogAPI
from aiopslab.observer.metric_api import PrometheusAPI
from aiopslab.observer import monitor_config, root_path


def collect_traces(start_time, end_time):
    tracer = TraceAPI(namespace=monitor_config["namespace"])
    traces = tracer.extract_traces(start_time, end_time)
    df_traces = tracer.process_traces(traces)
    save_path = root_path / "trace_output"
    tracer.save_traces(df_traces, save_path)


def collect_logs(start_time, end_time):
    logger = LogAPI(
        monitor_config["api"], monitor_config["username"], monitor_config["password"]
    )
    save_path = root_path / "log_output"
    os.makedirs(save_path, exist_ok=True)
    logger.log_extract(
        start_time=int(start_time.timestamp()),
        end_time=int(end_time.timestamp()),
        path=save_path,
    )


def collect_metrics(start_time, end_time):
    prom = PrometheusAPI(
        namespace=monitor_config["namespace"], url=monitor_config["prometheusApi"]
    )
    save_path = root_path / "metrics_output"
    prom.export_all_metrics(
        start_time=start_time, end_time=end_time, save_path=str(save_path), step=10
    )


def organize_collected_data():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_save_path = root_path / f"telemetry_data_{timestamp}"

    # Create the final directory
    os.makedirs(final_save_path, exist_ok=True)

    # Define source and destination paths
    log_src = root_path / "log_output"
    trace_src = root_path / "trace_output"
    metrics_src = root_path / "metrics_output"

    log_dest = final_save_path / "log"
    trace_dest = final_save_path / "trace"
    metrics_dest = final_save_path / "metric"

    def copy_latest_file(src_dir, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        items = list(Path(src_dir).glob("*"))
        if not items:
            print(f"No files or directories found in {src_dir}")
            return
        # Sort items by modified time and pick the latest
        latest_item = max(items, key=os.path.getmtime)

        if latest_item.is_dir():
            shutil.copytree(latest_item, dest_dir / latest_item.name)
            print(f"Copied directory {latest_item} to {dest_dir}")
        else:
            shutil.copy2(latest_item, dest_dir / latest_item.name)
            print(f"Copied file {latest_item} to {dest_dir}")

    if log_src.exists():
        copy_latest_file(log_src, log_dest)
    if trace_src.exists():
        copy_latest_file(trace_src, trace_dest)
    if metrics_src.exists():
        copy_latest_file(metrics_src, metrics_dest)


if __name__ == "__main__":
    end_time = datetime.now()
    start_time = end_time - timedelta(minutes=10)

    trace_thread = threading.Thread(target=collect_traces, args=(start_time, end_time))
    log_thread = threading.Thread(target=collect_logs, args=(start_time, end_time))
    metric_thread = threading.Thread(
        target=collect_metrics, args=(start_time, end_time)
    )

    trace_thread.start()
    log_thread.start()
    metric_thread.start()

    trace_thread.join()
    log_thread.join()
    metric_thread.join()

    organize_collected_data()

    print("Telemetry data collection completed successfully.")
