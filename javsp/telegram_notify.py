#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import requests
import html
from typing import Optional
from javsp.config import Cfg

logger = logging.getLogger(__name__)

class TelegramNotifier:
    """Telegram 通知类，用于发送影片整理完成的通知"""
    
    def __init__(self):
        """初始化 Telegram 通知器"""
        self.enabled = Cfg().telegram_config.enabled
        self.token = Cfg().telegram_config.token
        self.chat_id = Cfg().telegram_config.chat_id
        self.proxy = Cfg().telegram_config.proxy
        self.send_cover = Cfg().telegram_config.send_cover
        self.notification_level = Cfg().telegram_config.notification_level
        
        if self.enabled:
            if not self.token or not self.chat_id:
                logger.error("🔔 Telegram 通知已启用，但缺少 token 或 chat_id 配置")
                self.enabled = False
            else:
                logger.info("🔔 Telegram 通知已启用")
    
    def _escape_html(self, text: str) -> str:
        """转义HTML特殊字符
        
        Args:
            text: 要转义的文本
            
        Returns:
            str: 转义后的文本
        """
        return html.escape(text, quote=False)
    
    def _send_message(self, text: str, photo_path: Optional[str] = None) -> bool:
        """发送消息到 Telegram
        
        Args:
            text: 要发送的文本消息
            photo_path: 要发送的图片路径
            
        Returns:
            bool: 发送是否成功
        """
        if not self.enabled:
            return False
            
        proxies = None
        if self.proxy:
            proxies = {
                'http': self.proxy,
                'https': self.proxy
            }
        
        try:
            api_url = f"https://api.telegram.org/bot{self.token}"
            
            if photo_path and os.path.exists(photo_path) and self.send_cover:
                # 发送带有图片的消息
                with open(photo_path, 'rb') as photo:
                    files = {'photo': photo}
                    data = {'chat_id': self.chat_id, 'caption': text, 'parse_mode': 'HTML'}
                    response = requests.post(
                        f"{api_url}/sendPhoto", 
                        files=files, 
                        data=data,
                        proxies=proxies
                    )
            else:
                # 只发送文本消息
                data = {'chat_id': self.chat_id, 'text': text, 'parse_mode': 'HTML'}
                response = requests.post(
                    f"{api_url}/sendMessage", 
                    json=data,
                    proxies=proxies
                )
            
            if response.status_code == 200:
                logger.debug(f"🔔 Telegram 通知发送成功")
                return True
            else:
                logger.error(f"🔔 Telegram 通知发送失败: {response.status_code} {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"🔔 Telegram 通知发送异常: {str(e)}")
            return False
    
    def send_success_notification(self, movie_title: str, movie_id: str, save_dir: str, poster_path: Optional[str] = None) -> bool:
        """发送影片整理成功的通知
        
        Args:
            movie_title: 影片标题
            movie_id: 影片番号
            save_dir: 保存目录
            poster_path: 海报路径
            
        Returns:
            bool: 发送是否成功
        """
        if not self.enabled or self.notification_level == "error":
            return False
            
        # 尝试从 movie.info 获取更多信息
        actresses = ""
        producer = ""
        publish_date = ""
        
        # 从 javsp/__main__.py 中获取当前处理的影片对象，但保持健壮性
        try:
            from javsp.func import get_current_movie_info
            movie_info = get_current_movie_info()
            if movie_info:
                if hasattr(movie_info, 'actress') and movie_info.actress:
                    actresses = "👩 <b>演员</b>: " + self._escape_html(", ".join(movie_info.actress)) + "\n"
                if hasattr(movie_info, 'producer') and movie_info.producer:
                    producer = "🏢 <b>制作商</b>: " + self._escape_html(movie_info.producer) + "\n"
                if hasattr(movie_info, 'publish_date') and movie_info.publish_date:
                    publish_date = "📅 <b>发行日期</b>: " + self._escape_html(str(movie_info.publish_date)) + "\n"
        except:
            pass  # 即使获取额外信息失败也继续发送通知
        
        # 构建消息文本，确保所有内容都被HTML转义
        message = (
            f"✅ <b>影片整理完成</b>\n\n"
            f"🎬 <b>番号</b>: {self._escape_html(movie_id)}\n"
            f"📝 <b>标题</b>: {self._escape_html(movie_title)}\n"
            f"{actresses}"
            f"{producer}"
            f"{publish_date}"
            f"📁 <b>路径</b>: {self._escape_html(save_dir)}"
        )
        
        return self._send_message(message, poster_path)
    
    def send_error_notification(self, movie_id: str, error_message: str) -> bool:
        """发送影片整理失败的通知
        
        Args:
            movie_id: 影片番号
            error_message: 错误信息
            
        Returns:
            bool: 发送是否成功
        """
        if not self.enabled or self.notification_level == "success":
            return False
            
        # 构建消息文本
        message = (
            f"❌ <b>影片整理失败</b>\n\n"
            f"🎬 <b>番号</b>: {self._escape_html(movie_id)}\n"
            f"⚠️ <b>错误</b>: {self._escape_html(error_message)}"
        )
        
        return self._send_message(message)
        
    def send_batch_summary(self, total: int, success: int, failed: int) -> bool:
        """发送批量整理完成的汇总通知
        
        Args:
            total: 总影片数
            success: 成功数量
            failed: 失败数量
            
        Returns:
            bool: 发送是否成功
        """
        if not self.enabled:
            return False
            
        # 构建消息文本
        message = (
            f"📊 <b>批量整理完成</b>\n\n"
            f"🎬 <b>总数</b>: {total}\n"
            f"✅ <b>成功</b>: {success}\n"
            f"❌ <b>失败</b>: {failed}"
        )
        
        return self._send_message(message)

# 全局通知器实例
notifier = TelegramNotifier() 