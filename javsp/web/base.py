"""网络请求的统一接口"""
import os
import sys
import time
import shutil
import logging
import requests
import contextlib
import cloudscraper
import lxml.html
from tqdm import tqdm
from lxml import etree
from lxml.html.clean import Cleaner
from requests.models import Response


from javsp.config import Cfg
from javsp.web.exceptions import *


__all__ = ['Request', 'get_html', 'post_html', 'request_get', 'resp2html', 'is_connectable', 'download', 'get_resp_text', 'read_proxy']


headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'}

logger = logging.getLogger(__name__)
# 删除js脚本相关的tag，避免网页检测到没有js运行环境时强行跳转，影响调试
cleaner = Cleaner(kill_tags=['script', 'noscript'])

def read_proxy():
    if Cfg().network.proxy_server is None:
        return {}
    else:
        proxy = str(Cfg().network.proxy_server)
        return {'http': proxy, 'https': proxy}

# 与网络请求相关的功能汇总到一个模块中以方便处理，但是不同站点的抓取器又有自己的需求（针对不同网站
# 需要使用不同的UA、语言等）。每次都传递参数很麻烦，而且会面临函数参数越加越多的问题。因此添加这个
# 处理网络请求的类，它带有默认的属性，但是也可以在各个抓取器模块里进行进行定制
class Request():
    """作为网络请求出口并支持各个模块定制功能"""
    def __init__(self, use_scraper=False) -> None:
        # 必须使用copy()，否则各个模块对headers的修改都将会指向本模块中定义的headers变量，导致只有最后一个对headers的修改生效
        self.headers = headers.copy()
        self.cookies = {}

        self.proxies = read_proxy()
        self.timeout = Cfg().network.timeout.total_seconds()
        if not use_scraper:
            self.scraper = None
            self.__get = requests.get
            self.__post = requests.post
            self.__head = requests.head
        else:
            self.scraper = cloudscraper.create_scraper()
            self.__get = self._scraper_monitor(self.scraper.get)
            self.__post = self._scraper_monitor(self.scraper.post)
            self.__head = self._scraper_monitor(self.scraper.head)

    def _scraper_monitor(self, func):
        """监控cloudscraper的工作状态，遇到不支持的Challenge时尝试退回常规的requests请求"""
        def wrapper(*args, **kw):
            try:
                return func(*args, **kw)
            except Exception as e:
                logger.debug(f"无法通过CloudFlare检测: '{e}', 尝试退回常规的requests请求")
                if func == self.scraper.get:
                    return requests.get(*args, **kw)
                else:
                    return requests.post(*args, **kw)
        return wrapper

    def get(self, url, delay_raise=False):
        r = self.__get(url,
                      headers=self.headers,
                      proxies=self.proxies,
                      cookies=self.cookies,
                      timeout=self.timeout)
        if not delay_raise:
            r.raise_for_status()
        return r

    def post(self, url, data, delay_raise=False):
        r = self.__post(url,
                      data=data,
                      headers=self.headers,
                      proxies=self.proxies,
                      cookies=self.cookies,
                      timeout=self.timeout)
        if not delay_raise:
            r.raise_for_status()
        return r

    def head(self, url, delay_raise=True):
        r = self.__head(url,
                      headers=self.headers,
                      proxies=self.proxies,
                      cookies=self.cookies,
                      timeout=self.timeout)
        if not delay_raise:
            r.raise_for_status()
        return r

    def get_html(self, url):
        r = self.get(url)
        html = resp2html(r)
        return html


class DownloadProgressBar(tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


def request_get(url, cookies={}, timeout=None, delay_raise=False):
    """获取指定url的原始请求"""
    if timeout is None:
        timeout = Cfg().network.timeout.seconds
    
    logger.debug(f"🌐 发送请求: GET {url}")
    start_time = time.time()
    r = requests.get(url, headers=headers, proxies=read_proxy(), cookies=cookies, timeout=timeout)
    elapsed = time.time() - start_time
    
    # 记录响应信息
    status_emoji = "✅" if r.status_code < 400 else "❌"
    logger.debug(f"{status_emoji} 响应状态: {r.status_code} ({round(elapsed*1000)}ms) - {url}")
    
    if not delay_raise:
        if r.status_code == 403 and b'>Just a moment...<' in r.content:
            logger.error(f"🚫 CloudFlare 阻止: {url}")
            raise SiteBlocked(f"403 Forbidden: 无法通过CloudFlare检测: {url}")
        else:
            r.raise_for_status()
    return r


def request_post(url, data, cookies={}, timeout=None, delay_raise=False):
    """向指定url发送post请求"""
    if timeout is None:
        timeout = Cfg().network.timeout.seconds
    
    logger.debug(f"🌐 发送请求: POST {url}")
    start_time = time.time()
    r = requests.post(url, data=data, headers=headers, proxies=read_proxy(), cookies=cookies, timeout=timeout)
    elapsed = time.time() - start_time
    
    # 记录响应信息
    status_emoji = "✅" if r.status_code < 400 else "❌"
    logger.debug(f"{status_emoji} 响应状态: {r.status_code} ({round(elapsed*1000)}ms) - {url}")
    
    if not delay_raise:
        r.raise_for_status()
    return r


def get_resp_text(resp: Response, encoding=None):
    """提取Response的文本"""
    if encoding:
        resp.encoding = encoding
    else:
        resp.encoding = resp.apparent_encoding
    return resp.text


def get_html(url, encoding='utf-8'):
    """使用get方法访问指定网页并返回经lxml解析后的document"""
    resp = request_get(url)
    text = get_resp_text(resp, encoding=encoding)
    html = lxml.html.fromstring(text)
    html.make_links_absolute(url, resolve_base_href=True)
    # 清理功能仅应在需要的时候用来调试网页（如prestige），否则可能反过来影响调试（如JavBus）
    # html = cleaner.clean_html(html)
    if hasattr(sys, 'javsp_debug_mode'):
        lxml.html.open_in_browser(html, encoding=encoding)  # for develop and debug
    return html


def resp2html(resp, encoding='utf-8') -> lxml.html.HtmlComment:
    """将request返回的response转换为经lxml解析后的document"""
    text = get_resp_text(resp, encoding=encoding)
    html = lxml.html.fromstring(text)
    html.make_links_absolute(resp.url, resolve_base_href=True)
    # html = cleaner.clean_html(html)
    if hasattr(sys, 'javsp_debug_mode'):
        lxml.html.open_in_browser(html, encoding=encoding)  # for develop and debug
    return html


def post_html(url, data, encoding='utf-8', cookies={}):
    """使用post方法访问指定网页并返回经lxml解析后的document"""
    resp = request_post(url, data, cookies=cookies)
    text = get_resp_text(resp, encoding=encoding)
    html = lxml.html.fromstring(text)
    # jav321提供ed2k形式的资源链接，其中的非ASCII字符可能导致转换失败，因此要先进行处理
    ed2k_tags = html.xpath("//a[starts-with(@href,'ed2k://')]")
    for tag in ed2k_tags:
        tag.attrib['ed2k'], tag.attrib['href'] = tag.attrib['href'], ''
    html.make_links_absolute(url, resolve_base_href=True)
    for tag in ed2k_tags:
        tag.attrib['href'] = tag.attrib['ed2k']
        tag.attrib.pop('ed2k')
    # html = cleaner.clean_html(html)
    # lxml.html.open_in_browser(html, encoding=encoding)  # for develop and debug
    return html


def dump_xpath_node(node, filename=None):
    """将xpath节点dump到文件"""
    if not filename:
        filename = node.tag + '.html'
    with open(filename, 'wt', encoding='utf-8') as f:
        content = etree.tostring(node, pretty_print=True).decode('utf-8')
        f.write(content)


def is_connectable(url, timeout=3):
    """测试与指定url的连接"""
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        return True
    except requests.exceptions.RequestException as e:
        logger.debug(f"Not connectable: {url}\n" + repr(e))
        return False


def urlretrieve(url, filename=None, reporthook=None, headers=None):
    if "arzon" in url:
        headers["Referer"] = "https://www.arzon.jp/"
    """使用requests实现urlretrieve"""
    # https://blog.csdn.net/qq_38282706/article/details/80253447
    with contextlib.closing(requests.get(url, headers=headers,
                                         proxies=read_proxy(), stream=True)) as r:
        header = r.headers
        with open(filename, 'wb+') as fp:
            bs = 1024
            size = -1
            blocknum = 0
            if "content-length" in header:
                size = int(header["Content-Length"])    # 文件总大小（理论值）
            if reporthook:                              # 写入前运行一次回调函数
                reporthook(blocknum, bs, size)
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    fp.write(chunk)
                    fp.flush()
                    blocknum += 1
                    if reporthook:
                        reporthook(blocknum, bs, size)  # 每写入一次运行一次回调函数


def download(url, output_path, desc=None):
    """下载指定url的资源"""
    # 支持"下载"本地资源，以供fc2fan的本地镜像所使用
    if not url.startswith('http'):
        start_time = time.time()
        logger.debug(f"📋 复制本地文件: {url} -> {output_path}")
        shutil.copyfile(url, output_path)
        filesize = os.path.getsize(url)
        elapsed = time.time() - start_time
        info = {'total': filesize, 'elapsed': elapsed, 'rate': filesize/elapsed}
        logger.debug(f"✅ 复制完成: {filesize/1024/1024:.2f}MB, 耗时: {elapsed:.2f}秒")
        return info
    if not desc:
        desc = url.split('/')[-1]
    
    logger.debug(f"⬇️ 开始下载: {desc} -> {output_path}")
    referrer = headers.copy()
    referrer['referer'] = url[:url.find('/', 8)+1]  # 提取base_url部分
    with DownloadProgressBar(unit='B', unit_scale=True,
                             miniters=1, desc=desc, leave=False) as t:
        urlretrieve(url, filename=output_path, reporthook=t.update_to, headers=referrer)
        info = {k: t.format_dict[k] for k in ('total', 'elapsed', 'rate')}
        size_mb = info['total'] / 1024 / 1024
        rate_mb = info['rate'] / 1024 / 1024
        logger.debug(f"✅ 下载完成: {desc} ({size_mb:.2f}MB), 速度: {rate_mb:.2f}MB/s, 耗时: {info['elapsed']:.2f}秒")
        return info


def open_in_chrome(url, new=0, autoraise=True):
    """使用指定的Chrome Profile打开url，便于调试"""
    import subprocess
    chrome = R'C:\Program Files\Google\Chrome\Application\chrome.exe'
    subprocess.run(f'"{chrome}" --profile-directory="Profile 2" {url}', shell=True)

import webbrowser
webbrowser.open = open_in_chrome


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    download('https://www.javbus.com/pics/cover/6n54_b.jpg', 'cover.jpg')
