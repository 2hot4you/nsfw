import os
import re
import sys
import json
import time
import logging
from PIL import Image
from pydantic import ValidationError
from pydantic_extra_types.pendulum_dt import Duration
import requests
import threading
from typing import Dict, List
import datetime
import shutil

sys.stdout.reconfigure(encoding='utf-8')

import colorama
import pretty_errors
from colorama import Fore, Style
from tqdm import tqdm

from javsp.print import TqdmOut

# 配置日志格式，使其更加美观
class ColoredFormatter(logging.Formatter):
    """自定义的彩色日志格式化器"""
    COLORS = {
        'DEBUG': '\033[94m',     # 蓝色
        'INFO': '\033[92m',      # 绿色
        'WARNING': '\033[93m',   # 黄色
        'ERROR': '\033[91m',     # 红色
        'CRITICAL': '\033[95m',  # 紫色
        'RESET': '\033[0m'       # 重置
    }

    def format(self, record):
        # 获取原始的日志消息格式
        log_message = super().format(record)
        # 添加颜色
        if record.levelname in self.COLORS:
            log_message = f"{self.COLORS[record.levelname]}{log_message}{self.COLORS['RESET']}"
        return log_message

# 设置日志格式和级别
def setup_logging():
    """配置日志系统"""
    root_logger = logging.getLogger()
    
    # 清除现有的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # 创建控制台处理器
    console_handler = logging.StreamHandler(TqdmOut)
    
    # 设置格式
    log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    formatter = ColoredFormatter(log_format, date_format)
    console_handler.setFormatter(formatter)
    
    # 添加处理器
    root_logger.addHandler(console_handler)
    
    # 设置日志级别
    root_logger.setLevel(logging.INFO)
    
    # 创建日志目录
    os.makedirs('logs', exist_ok=True)
    
    # 添加文件处理器
    current_time = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    file_handler = logging.FileHandler(f'logs/javsp_{current_time}.log', encoding='utf-8')
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(file_handler)
    
    return root_logger

pretty_errors.configure(display_link=True)

# 将StreamHandler的stream修改为TqdmOut，以与Tqdm协同工作
root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if type(handler) == logging.StreamHandler:
        handler.stream = TqdmOut

# 设置日志
root_logger = setup_logging()
logger = logging.getLogger('main')

from javsp.cropper import Cropper, get_cropper

from javsp.lib import resource_path
from javsp.nfo import write_nfo
from javsp.file import *
from javsp.func import *
from javsp.image import *
from javsp.datatype import Movie, MovieInfo
from javsp.web.base import download
from javsp.web.exceptions import *
from javsp.web.translate import translate_movie_info
from javsp.telegram_notify import notifier  # 导入 Telegram 通知模块
from javsp.func import set_current_movie_info, get_current_movie_info  # 导入影片信息共享函数

from javsp.config import Cfg, CrawlerID
from javsp.prompt import prompt

actressAliasMap = {}

def resolve_alias(name):
    """将别名解析为固定的名字"""
    for fixedName, aliases in actressAliasMap.items():
        if name in aliases:
            return fixedName
    return name  # 如果找不到别名对应的固定名字，则返回原名


def import_crawlers():
    """按配置文件的抓取器顺序将该字段转换为抓取器的函数列表"""
    unknown_mods = []
    for _, mods in Cfg().crawler.selection.items():
        valid_mods = []
        for name in mods:
            try:
                # 导入fc2fan抓取器的前提: 配置了fc2fan的本地路径
                # if name == 'fc2fan' and (not os.path.isdir(Cfg().Crawler.fc2fan_local_path)):
                #     logger.debug('由于未配置有效的fc2fan路径，已跳过该抓取器')
                #     continue
                import_name = 'javsp.web.' + name
                __import__(import_name)
                valid_mods.append(import_name)  # 抓取器有效: 使用完整模块路径，便于程序实际使用
            except ModuleNotFoundError:
                unknown_mods.append(name)       # 抓取器无效: 仅使用模块名，便于显示
    if unknown_mods:
        logger.warning('配置的抓取器无效: ' + ', '.join(unknown_mods))


# 爬虫是IO密集型任务，可以通过多线程提升效率
def parallel_crawler(movie: Movie, tqdm_bar=None):
    """使用多线程抓取不同网站的数据"""
    def wrapper(parser, info: MovieInfo, retry):
        """对抓取器函数进行包装，便于更新提示信息和自动重试"""
        crawler_name = threading.current_thread().name
        task_info = f'Crawler: {crawler_name}: {info.dvdid}'
        for cnt in range(retry):
            try:
                parser(info)
                movie_id = info.dvdid or info.cid
                logger.debug(f"🎬 {crawler_name}: 抓取成功 '{movie_id}' ✅")
                logger.debug(f"🔗 {crawler_name}: 来源地址 '{info.url}'")
                setattr(info, 'success', True)
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'🎬 {crawler_name}: 抓取完成')
                break
            except MovieNotFoundError as e:
                logger.debug(f"⚠️ {crawler_name}: 影片未找到 - {str(e)}")
                break
            except MovieDuplicateError as e:
                logger.exception(f"⚠️ {crawler_name}: 重复影片 - {str(e)}")
                break
            except (SiteBlocked, SitePermissionError, CredentialError) as e:
                logger.error(f"🚫 {crawler_name}: 站点访问受限 - {str(e)}")
                break
            except requests.exceptions.RequestException as e:
                logger.debug(f'🔄 {crawler_name}: 网络错误，重试中 ({cnt+1}/{retry})\n  原因: {repr(e)}')
                if isinstance(tqdm_bar, tqdm):
                    tqdm_bar.set_description(f'🔄 {crawler_name}: 网络错误，重试中')
            except Exception as e:
                logger.exception(f"❌ {crawler_name}: 未知错误 - {str(e)}")

    # 根据影片的数据源获取对应的抓取器
    crawler_mods: List[CrawlerID] = Cfg().crawler.selection[movie.data_src]

    all_info = {i.value: MovieInfo(movie) for i in crawler_mods}
    # 番号为cid但同时也有有效的dvdid时，也尝试使用普通模式进行抓取
    if movie.data_src == 'cid' and movie.dvdid:
        crawler_mods = crawler_mods + Cfg().crawler.selection.normal
        for i in all_info.values():
            i.dvdid = None
        for i in Cfg().crawler.selection.normal:
            all_info[i.value] = MovieInfo(movie.dvdid)
    thread_pool = []
    for mod_partial, info in all_info.items():
        mod = f"javsp.web.{mod_partial}"
        parser = getattr(sys.modules[mod], 'parse_data')
        # 将all_info中的info实例传递给parser，parser抓取完成后，info实例的值已经完成更新
        # TODO: 抓取器如果带有parse_data_raw，说明它已经自行进行了重试处理，此时将重试次数设置为1
        if hasattr(sys.modules[mod], 'parse_data_raw'):
            th = threading.Thread(target=wrapper, name=mod, args=(parser, info, 1))
        else:
            th = threading.Thread(target=wrapper, name=mod, args=(parser, info, Cfg().network.retry))
        th.start()
        thread_pool.append(th)
    # 等待所有线程结束
    timeout = Cfg().network.retry * Cfg().network.timeout.total_seconds()
    for th in thread_pool:
        th: threading.Thread
        th.join(timeout=timeout)
    # 根据抓取结果更新影片类型判定
    if movie.data_src == 'cid' and movie.dvdid:
        titles = [all_info[i].title for i in Cfg().crawler.selection[movie.data_src]]
        if any(titles):
            movie.dvdid = None
            all_info = {k: v for k, v in all_info.items() if k in Cfg().crawler.selection['cid']}
        else:
            logger.debug(f'自动更正影片数据源类型: {movie.dvdid} ({movie.cid}): normal')
            movie.data_src = 'normal'
            movie.cid = None
            all_info = {k: v for k, v in all_info.items() if k not in Cfg().crawler.selection['cid']}
    # 删除抓取失败的站点对应的数据
    all_info = {k:v for k,v in all_info.items() if hasattr(v, 'success')}
    for info in all_info.values():
        del info.success
    # 删除all_info中键名中的'web.'
    all_info = {k[4:]:v for k,v in all_info.items()}
    return all_info


def info_summary(movie: Movie, all_info: Dict[str, MovieInfo]):
    """汇总多个来源的在线数据生成最终数据"""
    final_info = MovieInfo(movie)
    logger.info(f"📊 开始汇总影片 {movie.dvdid or movie.cid} 的元数据")
    
    ########## 部分字段配置了专门的选取逻辑，先处理这些字段 ##########
    # genre
    if 'javdb' in all_info and all_info['javdb'].genre:
        logger.debug(f"🏷️ 使用 javdb 的标签分类")
        final_info.genre = all_info['javdb'].genre

    ########## 移除所有抓取器数据中，标题尾部的女优名 ##########
    if Cfg().summarizer.title.remove_trailing_actor_name:
        for name, data in all_info.items():
            old_title = data.title
            data.title = remove_trail_actor_in_title(data.title, data.actress)
            if old_title != data.title:
                logger.debug(f"📝 {name}: 从标题中移除女优名: '{old_title}' -> '{data.title}'")
                
    ########## 然后检查所有字段，如果某个字段还是默认值，则按照优先级选取数据 ##########
    # parser直接更新了all_info中的项目，而初始all_info是按照优先级生成的，已经符合配置的优先级顺序了
    # 按照优先级取出各个爬虫获取到的信息
    attrs = [i for i in dir(final_info) if not i.startswith('_')]
    covers, big_covers = [], []
    for name, data in all_info.items():
        absorbed = []
        # 遍历所有属性，如果某一属性当前值为空而爬取的数据中含有该属性，则采用爬虫的属性
        for attr in attrs:
            incoming = getattr(data, attr)
            current = getattr(final_info, attr)
            if attr == 'cover':
                if incoming and (incoming not in covers):
                    covers.append(incoming)
                    absorbed.append(f"{attr} ({len(covers)})")
            elif attr == 'big_cover':
                if incoming and (incoming not in big_covers):
                    big_covers.append(incoming)
                    absorbed.append(f"{attr} ({len(big_covers)})")
            elif attr == 'uncensored':
                if (current is None) and (incoming is not None):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
            else:
                if (not current) and (incoming):
                    setattr(final_info, attr, incoming)
                    absorbed.append(attr)
        if absorbed:
            logger.debug(f"📥 从 '{name}' 中获取了: " + ', '.join(absorbed))
    
    # 使用网站的番号作为番号
    if Cfg().crawler.respect_site_avid:
        id_weight = {}
        for name, data in all_info.items():
            if data.title:
                if movie.dvdid:
                    id_weight.setdefault(data.dvdid, []).append(name)
                else:
                    id_weight.setdefault(data.cid, []).append(name)
        # 根据权重选择最终番号
        if id_weight:
            id_weight = {k:v for k, v in sorted(id_weight.items(), key=lambda x:len(x[1]), reverse=True)}
            final_id = list(id_weight.keys())[0]
            sources = ', '.join(id_weight[final_id])
            if movie.dvdid:
                old_id = final_info.dvdid
                final_info.dvdid = final_id
                if old_id != final_id:
                    logger.debug(f"🔢 修正番号: {old_id} -> {final_id} (来源: {sources})")
            else:
                old_id = final_info.cid
                final_info.cid = final_id
                if old_id != final_id:
                    logger.debug(f"🔢 修正番号: {old_id} -> {final_id} (来源: {sources})")
    
    # javdb封面有水印，优先采用其他站点的封面
    javdb_cover = getattr(all_info.get('javdb'), 'cover', None)
    if javdb_cover is not None:
        match Cfg().crawler.use_javdb_cover:
            case UseJavDBCover.fallback:
                covers.remove(javdb_cover)
                covers.append(javdb_cover)
            case UseJavDBCover.no:
                covers.remove(javdb_cover)

    setattr(final_info, 'covers', covers)
    setattr(final_info, 'big_covers', big_covers)
    # 对cover和big_cover赋值，避免后续检查必须字段时出错
    if covers:
        final_info.cover = covers[0]
    if big_covers:
        final_info.big_cover = big_covers[0]
    ########## 部分字段放在最后进行检查 ##########
    # 特殊的 genre
    if final_info.genre is None:
        final_info.genre = []
    if movie.hard_sub:
        final_info.genre.append('内嵌字幕')
    if movie.uncensored:
        final_info.genre.append('无码流出/破解')

    # 女优别名固定
    if Cfg().crawler.normalize_actress_name and bool(final_info.actress_pics):
        final_info.actress = [resolve_alias(i) for i in final_info.actress]
        if final_info.actress_pics:
            final_info.actress_pics = {
                resolve_alias(key): value for key, value in final_info.actress_pics.items()
            }

    # 检查是否所有必需的字段都已经获得了值
    for attr in Cfg().crawler.required_keys:
        if not getattr(final_info, attr, None):
            logger.error(f"所有抓取器均未获取到字段: '{attr}'，抓取失败")
            return False
    # 必需字段均已获得了值：将最终的数据附加到movie
    movie.info = final_info
    return True

def generate_names(movie: Movie):
    """按照模板生成相关文件的文件名"""

    def legalize_path(path: str):
        """
            Windows下文件名中不能包含换行 #467
            所以这里对文件路径进行合法化
        """
        return ''.join(c for c in path if c not in {'\n'})

    info = movie.info
    # 准备用来填充命名模板的字典
    d = info.get_info_dic()

    if info.actress and len(info.actress) > Cfg().summarizer.path.max_actress_count:
        logging.debug('女优人数过多，按配置保留了其中的前n个: ' + ','.join(info.actress))
        actress = info.actress[:Cfg().summarizer.path.max_actress_count] + ['…']
    else:
        actress = info.actress
    d['actress'] = ','.join(actress) if actress else Cfg().summarizer.default.actress

    # 保存label供后面判断裁剪图片的方式使用
    setattr(info, 'label', d['label'].upper())
    # 处理字段：替换不能作为文件名的字符，移除首尾的空字符
    for k, v in d.items():
        d[k] = replace_illegal_chars(v.strip())

    # 生成nfo文件中的影片标题
    nfo_title = Cfg().summarizer.nfo.title_pattern.format(**d)
    setattr(info, 'nfo_title', nfo_title)
    
    # 使用字典填充模板，生成相关文件的路径（多分片影片要考虑CD-x部分）
    cdx = '' if len(movie.files) <= 1 else '-CD1'
    if hasattr(info, 'title_break'):
        title_break = info.title_break
    else:
        title_break = split_by_punc(d['title'])
    if hasattr(info, 'ori_title_break'):
        ori_title_break = info.ori_title_break
    else:
        ori_title_break = split_by_punc(d['rawtitle'])
    copyd = d.copy()

    def legalize_info():
        if movie.save_dir != None:
            movie.save_dir = legalize_path(movie.save_dir)
        if movie.nfo_file != None:
            movie.nfo_file = legalize_path(movie.nfo_file)
        if movie.fanart_file != None:
            movie.fanart_file = legalize_path(movie.fanart_file)
        if movie.poster_file != None:
            movie.poster_file = legalize_path(movie.poster_file)
        if d['title'] != copyd['title']:
            logger.info(f"自动截短标题为:\n{copyd['title']}")
        if d['rawtitle'] != copyd['rawtitle']:
            logger.info(f"自动截短原始标题为:\n{copyd['rawtitle']}")
        return

    copyd['num'] = copyd['num'] + movie.attr_str
    longest_ext = max((os.path.splitext(i)[1] for i in movie.files), key=len)
    for end in range(len(ori_title_break), 0, -1):
        copyd['rawtitle'] = replace_illegal_chars(''.join(ori_title_break[:end]).strip())
        for sub_end in range(len(title_break), 0, -1):
            copyd['title'] = replace_illegal_chars(''.join(title_break[:sub_end]).strip())
            if Cfg().summarizer.move_files:
                save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
                basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
            else:
                # 如果不整理文件，则保存抓取的数据到当前目录
                save_dir = os.path.dirname(movie.files[0])
                filebasename = os.path.basename(movie.files[0])
                ext = os.path.splitext(filebasename)[1]
                basename = filebasename.replace(ext, '')
            long_path = os.path.join(save_dir, basename+longest_ext)
            remaining = get_remaining_path_len(os.path.abspath(long_path))
            if remaining > 0:
                movie.save_dir = save_dir
                movie.basename = basename
                movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
                movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
                movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')
                return legalize_info()
    else:
        # 以防万一，当整理路径非常深或者标题起始很长一段没有标点符号时，硬性截短生成的名称
        copyd['title'] = copyd['title'][:remaining]
        copyd['rawtitle'] = copyd['rawtitle'][:remaining]
        # 如果不整理文件，则保存抓取的数据到当前目录
        if not Cfg().summarizer.move_files:
            save_dir = os.path.dirname(movie.files[0])
            filebasename = os.path.basename(movie.files[0])
            ext = os.path.splitext(filebasename)[1]
            basename = filebasename.replace(ext, '')
        else:
            save_dir = os.path.normpath(Cfg().summarizer.path.output_folder_pattern.format(**copyd)).strip()
            basename = os.path.normpath(Cfg().summarizer.path.basename_pattern.format(**copyd)).strip()
        movie.save_dir = save_dir
        movie.basename = basename

        movie.nfo_file = os.path.join(save_dir, Cfg().summarizer.nfo.basename_pattern.format(**copyd) + '.nfo')
        movie.fanart_file = os.path.join(save_dir, Cfg().summarizer.fanart.basename_pattern.format(**copyd) + '.jpg')
        movie.poster_file = os.path.join(save_dir, Cfg().summarizer.cover.basename_pattern.format(**copyd) + '.jpg')

        return legalize_info()

def reviewMovieID(all_movies, root):
    """人工检查每一部影片的番号"""
    count = len(all_movies)
    logger.info('进入手动模式检查番号: ')
    for i, movie in enumerate(all_movies, start=1):
        id = repr(movie)[7:-2]
        print(f'[{i}/{count}]\t{Fore.LIGHTMAGENTA_EX}{id}{Style.RESET_ALL}, 对应文件:')
        relpaths = [os.path.relpath(i, root) for i in movie.files]
        print('\n'.join(['  '+i for i in relpaths]))
        s = prompt("回车确认当前番号，或直接输入更正后的番号（如'ABC-123'或'cid:sqte00300'）", "更正后的番号")
        if not s:
            logger.info(f"已确认影片番号: {','.join(relpaths)}: {id}")
        else:
            s = s.strip()
            s_lc = s.lower()
            if s_lc.startswith(('cid:', 'cid=')):
                new_movie = Movie(cid=s_lc[4:])
                new_movie.data_src = 'cid'
                new_movie.files = movie.files
            elif s_lc.startswith('fc2'):
                new_movie = Movie(s)
                new_movie.data_src = 'fc2'
                new_movie.files = movie.files
            else:
                new_movie = Movie(s)
                new_movie.data_src = 'normal'
                new_movie.files = movie.files
            all_movies[i-1] = new_movie
            new_id = repr(new_movie)[7:-2]
            logger.info(f"已更正影片番号: {','.join(relpaths)}: {id} -> {new_id}")
        print()


SUBTITLE_MARK_FILE = Image.open(os.path.abspath(resource_path('image/sub_mark.png')))
UNCENSORED_MARK_FILE = Image.open(os.path.abspath(resource_path('image/unc_mark.png')))

def process_poster(movie: Movie):
    def should_use_ai_crop_match(label):
        for r in Cfg().summarizer.cover.crop.on_id_pattern:
            if re.match(r, label):
                return True
        return False
    crop_engine = None
    if (movie.info.uncensored or
       movie.data_src == 'fc2' or
       should_use_ai_crop_match(movie.info.label.upper())):
        crop_engine = Cfg().summarizer.cover.crop.engine
    cropper = get_cropper(crop_engine)
    fanart_image = Image.open(movie.fanart_file)
    fanart_cropped = cropper.crop(fanart_image)

    if Cfg().summarizer.cover.add_label:
        if movie.hard_sub:
            fanart_cropped = add_label_to_poster(fanart_cropped, SUBTITLE_MARK_FILE, LabelPostion.BOTTOM_RIGHT)
        if movie.uncensored:
            fanart_cropped = add_label_to_poster(fanart_cropped, UNCENSORED_MARK_FILE, LabelPostion.BOTTOM_LEFT)
    fanart_cropped.save(movie.poster_file)

def RunNormalMode(all_movies):
    """普通整理模式"""
    def check_step(result, msg='步骤错误'):
        """检查一个整理步骤的结果，并负责更新tqdm的进度"""
        if result:
            inner_bar.update()
        else:
            raise Exception(msg + '\n')

    outer_bar = tqdm(all_movies, desc='整理影片', ascii=True, leave=False)
    total_step = 6
    if Cfg().translator.engine:
        total_step += 1
    if Cfg().summarizer.extra_fanarts.enabled:
        total_step += 1

    return_movies = []
    success_count = 0
    failed_count = 0
    
    for movie in outer_bar:
        try:
            # 初始化本次循环要整理影片任务
            filenames = [os.path.split(i)[1] for i in movie.files]
            logger.info('正在整理: ' + ', '.join(filenames))
            inner_bar = tqdm(total=total_step, desc='步骤', ascii=True, leave=False)
            # 依次执行各个步骤
            inner_bar.set_description(f'启动并发任务')
            all_info = parallel_crawler(movie, inner_bar)
            msg = f'为其配置的{len(Cfg().crawler.selection[movie.data_src])}个抓取器均未获取到影片信息'
            check_step(all_info, msg)

            inner_bar.set_description('汇总数据')
            has_required_keys = info_summary(movie, all_info)
            check_step(has_required_keys)
            
            # 设置当前影片信息，供通知系统使用
            set_current_movie_info(movie.info)

            if Cfg().translator.engine:
                inner_bar.set_description('翻译影片信息')
                success = translate_movie_info(movie.info)
                check_step(success)

            generate_names(movie)
            check_step(movie.save_dir, '无法按命名规则生成目标文件夹')
            if not os.path.exists(movie.save_dir):
                os.makedirs(movie.save_dir)

            inner_bar.set_description('下载封面图片')
            if Cfg().summarizer.cover.highres:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file, movie.info.big_covers)
            else:
                cover_dl = download_cover(movie.info.covers, movie.fanart_file)
            check_step(cover_dl, '下载封面图片失败')
            cover, pic_path = cover_dl
            # 确保实际下载的封面的url与即将写入到movie.info中的一致
            if cover != movie.info.cover:
                movie.info.cover = cover
            # 根据实际下载的封面的格式更新fanart/poster等图片的文件名
            if pic_path != movie.fanart_file:
                movie.fanart_file = pic_path
                actual_ext = os.path.splitext(pic_path)[1]
                movie.poster_file = os.path.splitext(movie.poster_file)[0] + actual_ext

            process_poster(movie)

            check_step(True)

            if Cfg().summarizer.extra_fanarts.enabled:
                scrape_interval = Cfg().summarizer.extra_fanarts.scrap_interval.total_seconds()
                inner_bar.set_description('下载剧照')
                if movie.info.preview_pics:
                    extrafanartdir = movie.save_dir + '/extrafanart'
                    os.mkdir(extrafanartdir)
                    for (id, pic_url) in enumerate(movie.info.preview_pics):
                        inner_bar.set_description(f"Downloading extrafanart {id} from url: {pic_url}")
                                                                                                                                
                        fanart_destination = f"{extrafanartdir}/{id}.png"
                        try:
                            info = download(pic_url, fanart_destination)
                            if valid_pic(fanart_destination):
                                filesize = get_fmt_size(pic_path)
                                width, height = get_pic_size(pic_path)
                                elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                                speed = get_fmt_size(info['rate']) + '/s'
                                logger.info(f"已下载剧照{pic_url} {id}.png: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                            else:
                                check_step(False, f"下载剧照{id}: {pic_url}失败")
                        except:
                            check_step(False, f"下载剧照{id}: {pic_url}失败")
                        time.sleep(scrape_interval)
                check_step(True)

            inner_bar.set_description('写入NFO')
            write_nfo(movie.info, movie.nfo_file)
            check_step(True)
            if Cfg().summarizer.move_files:
                inner_bar.set_description('移动影片文件')
                movie.rename_files(Cfg().summarizer.path.hard_link)
                check_step(True)
                logger.info(f'整理完成，相关文件已保存到: {movie.save_dir}\n')
            else:
                logger.info(f'刮削完成，相关文件已保存到: {movie.nfo_file}\n')
            
            # 发送 Telegram 成功通知
            movie_id = movie.dvdid or movie.cid
            notifier.send_success_notification(
                movie_title=movie.info.title, 
                movie_id=movie_id,
                save_dir=movie.save_dir,
                poster_path=movie.poster_file
            )
            
            success_count += 1
            
            if movie != all_movies[-1] and Cfg().crawler.sleep_after_scraping > Duration(0):
                time.sleep(Cfg().crawler.sleep_after_scraping.total_seconds())
            return_movies.append(movie)
        except Exception as e:
            logger.debug(e, exc_info=True)
            logger.error(f'整理失败: {e}')
            
            # 发送 Telegram 失败通知
            movie_id = movie.dvdid or movie.cid
            notifier.send_error_notification(
                movie_id=movie_id,
                error_message=str(e)
            )
            
            failed_count += 1
        finally:
            # 清除当前影片信息
            set_current_movie_info(None)
            inner_bar.close()
    
    # 发送批量整理完成的汇总通知
    total_count = len(all_movies)
    notifier.send_batch_summary(
        total=total_count,
        success=success_count,
        failed=failed_count
    )
    
    return return_movies


def download_cover(covers, fanart_path, big_covers=[]):
    """下载封面图片"""
    # 优先下载高清封面
    for url in big_covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                info = download(url, pic_path)
                if valid_pic(pic_path):
                    filesize = get_fmt_size(pic_path)
                    width, height = get_pic_size(pic_path)
                    elapsed = time.strftime("%M:%S", time.gmtime(info['elapsed']))
                    speed = get_fmt_size(info['rate']) + '/s'
                    logger.info(f"已下载高清封面: {width}x{height}, {filesize} [{elapsed}, {speed}]")
                    return (url, pic_path)
            except requests.exceptions.HTTPError:
                # HTTPError通常说明猜测的高清封面地址实际不可用，因此不再重试
                break
    # 如果没有高清封面或高清封面下载失败
    for url in covers:
        pic_path = get_pic_path(fanart_path, url)
        for _ in range(Cfg().network.retry):
            try:
                download(url, pic_path)
                if valid_pic(pic_path):
                    logger.debug(f"已下载封面: '{url}'")
                    return (url, pic_path)
                else:
                    logger.debug(f"图片无效或已损坏: '{url}'，尝试更换下载地址")
                    break
            except Exception as e:
                logger.debug(e, exc_info=True)
    logger.error(f"下载封面图片失败")
    logger.debug('big_covers:'+str(big_covers) + ', covers'+str(covers))
    return None

def get_pic_path(fanart_path, url):
    fanart_base = os.path.splitext(fanart_path)[0]
    pic_extend = url.split('.')[-1]
    # 判断 url 是否带？后面的参数
    if '?' in pic_extend:
        pic_extend = pic_extend.split('?')[0]
        
    pic_path = fanart_base + "." + pic_extend
    return pic_path

def error_exit(success, err_info):
    """检查业务逻辑是否成功完成，如果失败则报错退出程序"""
    if not success:
        logger.error(err_info)
        sys.exit(1)


def entry():
    try:
        Cfg()
    except ValidationError as e:
        print(e.errors())
        exit(1)

    global actressAliasMap
    if Cfg().crawler.normalize_actress_name:
        actressAliasFilePath = resource_path("data/actress_alias.json")
        with open(actressAliasFilePath, "r", encoding="utf-8") as file:
            actressAliasMap = json.load(file)

    colorama.init(autoreset=True)

    # 检查更新
    version_info = 'JavSP ' + getattr(sys, 'javsp_version', '未知版本/从代码运行')
    logger.debug(version_info.center(60, '='))
    check_update(Cfg().other.check_update, Cfg().other.auto_update)
    root = get_scan_dir(Cfg().scanner.input_directory)
    error_exit(root, '未选择要扫描的文件夹')
    # 导入抓取器，必须在chdir之前
    import_crawlers()
    os.chdir(root)

    print(f'扫描影片文件...')
    recognized = scan_movies(root)
    movie_count = len(recognized)
    recognize_fail = []
    error_exit(movie_count, '未找到影片文件')
    logger.info(f'扫描影片文件：共找到 {movie_count} 部影片')
    if Cfg().scanner.manual:
        reviewMovieID(recognized, root)
    RunNormalMode(recognized + recognize_fail)

    sys.exit(0)

if __name__ == "__main__":
    entry()
