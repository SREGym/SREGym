# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.


from datetime import datetime, timedelta
import os
import pandas as pd
import shutil
import zipfile


def get_dates_and_timestamps(start, end):
    start_dt = datetime.fromtimestamp(start)
    end_dt = datetime.fromtimestamp(end)

    dates = []
    timestamps = []

    current_dt = start_dt
    while current_dt <= end_dt:
        dates.append(current_dt.strftime("%Y-%m-%d"))
        start_of_day = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = current_dt.replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
        timestamps.append([start_of_day.timestamp(), end_of_day.timestamp()])
        current_dt += timedelta(days=1)
    timestamps[0][0] = start
    timestamps[len(timestamps) - 1][1] = end
    return dates, timestamps


def merge_csv(path, csv_list, data_type):
    data_list = []
    for i, folder_path in enumerate(csv_list):
        data = pd.read_csv(folder_path)
        data_list.append(data)
        os.remove(folder_path)
    all_data = pd.concat(data_list)
    all_data = all_data.reset_index(drop=True)
    all_data.to_csv(f"{path}/{data_type}.csv")


def delete_folder(folder_path):
    if os.path.exists(folder_path):
        try:
            shutil.rmtree(folder_path)
        except OSError as e:
            pass
    else:
        pass


def zip_dir(path):
    zip_path = f"{path}.zip"
    file_count = sum(len(files) for _, _, files in os.walk(path))
    with zipfile.ZipFile(zip_path, "w") as zip_file:
        for root, _, files in os.walk(path):
            for file in files:
                file_path = os.path.join(root, file)
                zip_file.write(file_path, os.path.relpath(file_path, path))
