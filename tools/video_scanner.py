#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import yaml
import schedule
import time
from datetime import datetime, timedelta
import requests
from pathlib import Path
import logging
import argparse
from typing import Dict, List, Optional
import psutil
import humanize
from functools import wraps
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/video_scanner.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

def retry_on_error(max_retries=3, delay=1):
    """错误重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries == max_retries:
                        logging.error(f"函数 {func.__name__} 执行失败，已重试 {max_retries} 次: {e}")
                        raise
                    logging.warning(f"函数 {func.__name__} 执行失败，{delay} 秒后重试 ({retries}/{max_retries}): {e}")
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

class VideoScanner:
    def __init__(self, config_path: str = 'config.yml'):
        self.config = self._load_config(config_path)
        self.telegram_config = self.config.get('telegram_config', {})
        self.scanner_config = self.config.get('scanner', {})
        self.input_directory = self.scanner_config.get('input_directory')
        self.extensions = self.scanner_config.get('filename_extensions', [])
        self.ignored_folders = self.scanner_config.get('ignored_folder_name_pattern', [])
        self.start_time = None
        self.process = psutil.Process()

    def _load_config(self, config_path: str) -> Dict:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logging.error(f"加载配置文件失败: {e}")
            return {}

    def _should_ignore_folder(self, folder_name: str) -> bool:
        return any(folder_name.startswith(pattern.strip('^')) for pattern in self.ignored_folders)

    def _get_folder_size(self, folder_path: str) -> int:
        """获取文件夹大小"""
        total_size = 0
        for dirpath, _, filenames in os.walk(folder_path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
        return total_size

    @retry_on_error(max_retries=3)
    def count_videos(self) -> Dict[str, int]:
        """统计视频文件数量"""
        stats = {
            'total': 0,
            'new_today': 0,
            'total_size': 0,
            'subtitle_count': 0,
            'by_folder': {}
        }
        
        if not self.input_directory or not os.path.exists(self.input_directory):
            logging.error(f"输入目录不存在: {self.input_directory}")
            return stats

        # 获取所有视频文件扩展名
        video_extensions = tuple(ext.lower() for ext in self.extensions)
        # 获取所有字幕文件扩展名
        subtitle_extensions = tuple(ext.lower() for ext in self.scanner_config.get('subtitle_extensions', []))
        
        # 获取昨天的时间范围
        yesterday = datetime.now() - timedelta(days=1)
        yesterday_start = datetime(yesterday.year, yesterday.month, yesterday.day)
        yesterday_end = yesterday_start + timedelta(days=1)
        
        for root, dirs, files in os.walk(self.input_directory):
            # 跳过被忽略的文件夹
            dirs[:] = [d for d in dirs if not self._should_ignore_folder(d)]
            
            # 统计当前文件夹中的视频文件
            video_files = [f for f in files if f.lower().endswith(video_extensions)]
            video_count = len(video_files)
            
            # 统计当前文件夹中的字幕文件
            subtitle_files = [f for f in files if f.lower().endswith(subtitle_extensions)]
            subtitle_count = len(subtitle_files)
            
            if video_count > 0 or subtitle_count > 0:
                rel_path = os.path.relpath(root, self.input_directory)
                folder_stats = {
                    'count': video_count,
                    'subtitle_count': subtitle_count,
                    'size': 0,
                    'new_today': 0
                }
                
                # 统计文件大小和新增文件
                for file in video_files + subtitle_files:
                    file_path = os.path.join(root, file)
                    try:
                        file_size = os.path.getsize(file_path)
                        folder_stats['size'] += file_size
                        stats['total_size'] += file_size
                        
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                        if yesterday_start <= file_mtime < yesterday_end:
                            folder_stats['new_today'] += 1
                            stats['new_today'] += 1
                    except OSError as e:
                        logging.warning(f"无法访问文件 {file_path}: {e}")
                        continue
                
                stats['by_folder'][rel_path] = folder_stats
                stats['total'] += video_count
                stats['subtitle_count'] += subtitle_count

        return stats

    @retry_on_error(max_retries=3)
    def send_telegram_notification(self, stats: Dict[str, int]) -> None:
        """发送 Telegram 通知"""
        if not self.telegram_config.get('enabled'):
            return

        token = self.telegram_config.get('token')
        chat_id = self.telegram_config.get('chat_id')
        
        if not token or not chat_id:
            logging.error("Telegram 配置不完整")
            return

        # 计算扫描耗时
        scan_duration = time.time() - self.start_time
        memory_usage = self.process.memory_info().rss / 1024 / 1024  # MB

        # 构建消息
        message = f"🎬 *视频文件扫描报告*\n\n"
        message += f"📅 扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        message += f"📊 视频文件总数: {stats['total']} 个\n"
        message += f"💬 字幕文件总数: {stats['subtitle_count']} 个\n"
        message += f"💾 总大小: {humanize.naturalsize(stats['total_size'])}\n"
        message += f"🆕 昨日新增: {stats['new_today']} 个\n"
        message += f"⏱️ 扫描耗时: {scan_duration:.1f} 秒\n"
        message += f"🧠 内存使用: {memory_usage:.1f} MB\n"
        message += f"📁 扫描目录: {self.input_directory}"

        # 发送消息
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'Markdown'
            }
            response = requests.post(url, data=data)
            response.raise_for_status()
            logging.info("Telegram 通知发送成功")
        except Exception as e:
            logging.error(f"发送 Telegram 通知失败: {e}")

    def scan_and_notify(self) -> None:
        """执行扫描并发送通知"""
        self.start_time = time.time()
        logging.info("开始扫描视频文件...")
        stats = self.count_videos()
        self.send_telegram_notification(stats)
        logging.info("扫描完成")

def parse_args():
    parser = argparse.ArgumentParser(description='视频文件扫描器')
    parser.add_argument('--once', '-o',
                      action='store_true',
                      help='只运行一次，不设置定时任务')
    return parser.parse_args()

def main():
    args = parse_args()
    scanner = VideoScanner()
    
    if args.once:
        # 只运行一次并退出
        scanner.scan_and_notify()
        logging.info("扫描完成，程序退出")
        return
    
    # 从配置文件获取运行时间
    run_time = scanner.scanner_config.get('run_time', '02:00')
    delete_empty = scanner.scanner_config.get('delete_empty_folders', False)
    
    # 设置定时任务
    schedule.every().day.at(run_time).do(scanner.scan_and_notify)
    logging.info(f"已设置定时任务，将在每天 {run_time} 执行扫描")
    
    # 如果配置了删除空文件夹，则添加删除任务
    if delete_empty:
        schedule.every().day.at(run_time).do(scanner.delete_empty_folders)
        logging.info("已启用空文件夹删除功能")
    
    # 立即执行一次
    scanner.scan_and_notify()
    logging.info("首次扫描完成，程序退出")
    
    # 保持程序运行
    while True:
        schedule.run_pending()
        time.sleep(60)  # 每分钟检查一次是否有待执行的任务

if __name__ == "__main__":
    main() 