import datetime
import pytz
import threading
from typing import List, Tuple, Dict, Any

from app.core.context import Context
from app.core.event import eventmanager, Event
from app.schemas.types import EventType, MediaType
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.modules.themoviedb.tmdbapi import TmdbHelper
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.helper.sites import SitesHelper
from app.utils.string import StringUtils

class DownloadSiteTag(_PluginBase):
    # 插件名称
    plugin_name = "下载任务分类与标签"
    # 插件描述
    plugin_desc = "自动给下载任务分类与打站点标签、剧集名称标签"
    # 插件图标
    plugin_icon = "Youtube-dl_B.png"
    # 插件版本
    plugin_version = "1.7"
    # 插件作者
    plugin_author = "叮叮当"
    # 作者主页
    author_url = "https://github.com/cikezhu"
    # 插件配置项ID前缀
    plugin_config_prefix = "DownloadSiteTag_"
    # 加载顺序
    plugin_order = 2
    # 可使用的用户级别
    auth_level = 1
    # 日志前缀
    LOG_TAG = "[DownloadSiteTag] "
    
    # 退出事件
    _event = threading.Event()
    # 私有属性
    downloader_qb = None
    downloader_tr = None
    downloadhistory_oper = None
    tmdb_helper = None
    sites_helper = None
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _interval = "计划任务"
    _interval_cron = "5 4 * * *"
    _interval_time = 6
    _interval_unit = "小时"
    _enabled_media_tag = False
    _enabled_tag = True
    _enabled_category = False
    _category_movie = None
    _category_tv = None
    _category_anime = None

    def init_plugin(self, config: dict = None):
        self.downloader_qb = Qbittorrent()
        self.downloader_tr = Transmission()
        self.downloadhistory_oper = DownloadHistoryOper()
        self.tmdb_helper = TmdbHelper()
        self.sites_helper = SitesHelper()
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._interval = config.get("interval") or "计划任务"
            self._interval_cron = config.get("interval_cron") or "5 4 * * *"
            self._interval_time = self.str_to_number(config.get("interval_time"), 6)
            self._interval_unit = config.get("interval_unit") or "小时"
            self._enabled_media_tag = config.get("enabled_media_tag")
            self._enabled_tag = config.get("enabled_tag")
            self._enabled_category = config.get("enabled_category")
            self._category_movie = config.get("category_movie") or "电影"
            self._category_tv = config.get("category_tv") or "电视"
            self._category_anime = config.get("category_anime") or "动漫"
            if not ("interval_cron" in config):
                # 新版本v1.6更新插件配置默认配置
                config["interval"] = self._interval
                config["interval_cron"] =  self._interval_cron
                config["interval_time"] = self._interval_time
                config["interval_unit"] = self._interval_unit
                self.update_config(config)
                logger.warn(f"{self.LOG_TAG}新版本v{self.plugin_version} 配置修正 ...")
        
        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 创建定时任务控制器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            # 执行一次, 关闭onlyonce
            self._onlyonce = False
            config.update({"onlyonce": self._onlyonce})
            self.update_config(config)
            # 添加 补全下载历史的标签与分类 任务
            self._scheduler.add_job(func= self._complemented_history, trigger='date',
                                            run_date=datetime.datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                            )
        if self._enabled:
            if self._interval == "计划任务" or self._interval == "固定间隔":
                args = {}
                if self._interval == "固定间隔":
                    args["trigger"] = "interval"
                    if self._interval_unit == "小时":
                        args["hours"] = self._interval_time
                    else:
                        args["minutes"] = self._interval_time
                        if args["minutes"] < 5:
                            args["minutes"] = 5
                            logger.info(f"{self.LOG_TAG}启动定时服务: 最小不少于5分钟, 防止执行间隔太短任务冲突")
                else:
                    args["trigger"] = CronTrigger.from_crontab(self._interval_cron)
                try:
                    self._scheduler.add_job(func=lambda: self._complemented_history(interval=True),
                                        **args,
                                        name="补全下载历史的标签与分类")
                    logger.info(
                        f"{self.LOG_TAG}添加定时服务: 补全下载历史的标签与分类" + (f"(每){args.get('hours') or args.get('minutes')}{self._interval_unit}执行一次" if args["trigger"] == "interval" else f",计划任务: {self._interval_cron}"))
                except Exception as e:
                    logger.error(
                        f"{self.LOG_TAG}添加定时服务发生了错误: {str(e)}")

        if self._scheduler and self._scheduler.get_jobs():
            # 启动服务
            self._scheduler.print_jobs()
            self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    
    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def str_to_number(self, s: str, i: int) -> int:
        try:
            return int(s)
        except:
            return i

    def _complemented_history(self, interval: bool = False):
        """
        补全下载历史的标签与分类
        """
        logger.info(f"{self.LOG_TAG}开始执行{'(定时任务)' if interval else ''}: 补全下载历史的标签与分类 ...")
        # 记录处理的种子, 供辅种(无下载历史)使用
        dispose_history = {}
        for DOWNLOADER in ["qbittorrent", "transmission"]:
            logger.info(f"{self.LOG_TAG}开始扫描下载器 {DOWNLOADER} ...")
            # 获取下载器中的种子
            downloader_obj = self._get_downloader(DOWNLOADER)
            if not downloader_obj:
                logger.error(f"{self.LOG_TAG} 获取下载器失败 {DOWNLOADER}")
                continue
            torrents, error = downloader_obj.get_torrents()
            # 如果下载器获取种子发生错误 或 没有种子 则跳过
            if error or not torrents:
                continue
            logger.info(f"{self.LOG_TAG}按时间重新排序 {DOWNLOADER} 种子数：{len(torrents)}")
            # 按添加时间进行排序, 时间靠前的按大小和名称加入处理历史, 判定为原始种子, 其他为辅种
            torrents = self._torrents_sort(torrents=torrents, dl_type=DOWNLOADER)
            logger.info(f"{self.LOG_TAG}下载器 {DOWNLOADER} 分析种子信息中 ...")
            for torrent in torrents:
                try:
                    if self._event.is_set():
                        logger.info(f"{self.LOG_TAG}停止服务{'(定时任务)' if interval else ''}: 补全下载历史的标签与分类")
                        return
                    # 获取已处理种子的key (size, name)
                    _key = self._torrent_key(torrent=torrent, dl_type=DOWNLOADER)
                    # 获取种子hash
                    _hash = self._get_hash(torrent=torrent, dl_type=DOWNLOADER)
                    if not _hash:
                        continue
                    # 获取种子当前标签
                    torrent_tags = self._get_label(torrent=torrent, dl_type=DOWNLOADER)
                    torrent_cat = self._get_category(torrent=torrent, dl_type=DOWNLOADER)
                    # 提取种子hash对应的下载历史
                    history: DownloadHistory = self.downloadhistory_oper.get_by_hash(_hash)
                    if not history:
                        # 如果找到已处理种子的历史, 表明当前种子是辅种, 否则创建一个空DownloadHistory
                        if _key and _key in dispose_history:
                            history = dispose_history[_key]
                            # 因为辅种站点必定不同, 所以需要更新站点名字 history.torrent_site
                            history.torrent_site = None
                        else:
                            history = DownloadHistory(
                                torrent_site=None,
                                title=None,
                                type=None,
                                tmdbid=None)
                    else:
                        # 加入历史记录
                        if _key:
                            dispose_history[_key] = history
                    # 获取已知索引列表
                    indexers_list = [v.get("name") for k, v in (self.sites_helper._indexers or {}).items()]
                    # JackettIndexers索引器支持多个站点, 如果不存在历史记录, 则通过tracker会再次附加其他站点名称
                    indexers_list.append("JackettIndexers")
                    # 如果标签已经存在任意站点, 则不再添加站点标签
                    if set(indexers_list).intersection(set(torrent_tags)):
                        history.torrent_site = None
                    # 如果站点名称为空, 尝试通过trackers识别
                    elif not history.torrent_site:
                        trackers = self._get_trackers(torrent=torrent, dl_type=DOWNLOADER)
                        for tracker in trackers:
                            domain = StringUtils.get_url_domain(tracker)
                            site_info = self.sites_helper.get_indexer(domain)
                            if site_info:
                                history.torrent_site = site_info.get("name")
                                break
                        # 如果通过tracker还是无法获取站点名称, 且tmdbid, type, title都是空的, 那么跳过当前种子
                        if not history.torrent_site and not history.tmdbid and not history.type and not history.title:
                            continue
                    # 按设置生成需要写入的标签与分类
                    _tags = []
                    _cat = None
                    # 站点标签, 如果勾选开关的话 因允许torrent_site为空时运行到此, 因此需要判断torrent_site不为空
                    if self._enabled_tag and history.torrent_site:
                        _tags.append(history.torrent_site)
                    # 媒体标题标签, 如果勾选开关的话 因允许title为空时运行到此, 因此需要判断title不为空
                    if self._enabled_media_tag and history.title:
                        _tags.append(history.title)
                    # 分类, 如果勾选开关的话 <tr暂不支持> 因允许mtype为空时运行到此, 因此需要判断mtype不为空。为防止不必要的识别, 种子已经存在分类torrent_cat时 也不执行
                    if DOWNLOADER == "qbittorrent" and self._enabled_category and not torrent_cat and history.type:
                        # 如果是电视剧 需要区分是否动漫
                        genre_ids = None
                        # 因允许tmdbid为空时运行到此, 因此需要判断tmdbid不为空
                        if history.tmdbid and (history.type == MediaType.TV or history.type == MediaType.TV.value):
                            # tmdb_id获取tmdb信息
                            tmdb_info = self.tmdb_helper.get_info(mtype=history.type, tmdbid=history.tmdbid)
                            if tmdb_info:
                                genre_ids = tmdb_info.get("genre_ids")
                        _cat = self._genre_ids_get_cat(history.type, genre_ids)
                    
                    # 去除种子已经存在的标签
                    if _tags and torrent_tags:
                        _tags = list(set(_tags) - set(torrent_tags))
                    # 如果分类一样, 那么不需要修改
                    if _cat == torrent_cat:
                        _cat = None
                    # 判断当前种子是否不需要修改
                    if not _cat and not _tags:
                        continue
                    # 执行通用方法, 设置种子标签与分类
                    self._set_torrent_info(DOWNLOADER=DOWNLOADER, _hash=_hash, _torrent=torrent, _tags=_tags, _cat=_cat, _original_tags=torrent_tags)
                except Exception as e:
                    logger.error(
                        f"{self.LOG_TAG}分析种子信息时发生了错误: {str(e)}")


        logger.info(f"{self.LOG_TAG}执行完成{'(定时任务)' if interval else ''}: 补全下载历史的标签与分类 ...")

    def _genre_ids_get_cat(self, mtype, genre_ids = None):
        """
        根据genre_ids判断是否<动漫>分类
        """
        _cat = None
        if mtype == MediaType.MOVIE or mtype == MediaType.MOVIE.value:
            # 电影
            _cat = self._category_movie
        elif mtype:
            ANIME_GENREIDS = settings.ANIME_GENREIDS
            if genre_ids \
                    and set(genre_ids).intersection(set(ANIME_GENREIDS)):
                # 动漫
                _cat = self._category_anime
            else:
                # 电视剧
                _cat = self._category_tv
        return _cat

    def _get_downloader(self, dtype: str):
        """
        根据类型返回下载器实例
        """
        if dtype == "qbittorrent":
            return self.downloader_qb
        elif dtype == "transmission":
            return self.downloader_tr
        else:
            return None

    def _torrent_key(self, torrent: Any, dl_type: str):
        """
        按种子大小和时间返回key
        """
        size = None
        name = None
        if dl_type == "qbittorrent":
            size = torrent.get('size')
            name = torrent.get('name')
        else:
            size = torrent.total_size
            name = torrent.name
        if not size or not name:
            return None
        else:
            return (size, name)

    def _torrents_sort(self, torrents: Any, dl_type: str):
        """
        按种子添加时间排序
        """
        if dl_type == "qbittorrent":
            torrents = sorted(torrents, key=lambda x: x.get("added_on"), reverse=False)
        else:
            torrents = sorted(torrents, key=lambda x: x.added_date, reverse=False)
        return torrents

    def _get_hash(self, torrent: Any, dl_type: str):
        """
        获取种子hash
        """
        try:
            return torrent.get("hash") if dl_type == "qbittorrent" else torrent.hashString
        except Exception as e:
            print(str(e))
            return ""

    def _get_trackers(self, torrent: Any, dl_type: str):
        """
        获取种子trackers
        """
        try:
            if dl_type == "qbittorrent":
                """
                url	字符串	跟踪器网址
                status	整数	跟踪器状态。有关可能的值，请参阅下表
                tier	整数	跟踪器优先级。较低级别的跟踪器在较高级别的跟踪器之前试用。当特殊条目（如 DHT）不存在时，层号用作占位符时，层号有效。>= 0< 0tier
                num_peers	整数	跟踪器报告的当前 torrent 的对等体数量
                num_seeds	整数	当前种子的种子数，由跟踪器报告
                num_leeches	整数	当前种子的水蛭数量，如跟踪器报告的那样
                num_downloaded	整数	跟踪器报告的当前 torrent 的已完成下载次数
                msg	字符串	跟踪器消息（无法知道此消息是什么 - 由跟踪器管理员决定）
                """
                return [tracker.get("url") for tracker in (torrent.trackers or []) if tracker.get("tier", -1) >= 0 and tracker.get("url")]
            else:
                """
                class Tracker(Container):
                    @property
                    def id(self) -> int:
                        return self.fields["id"]

                    @property
                    def announce(self) -> str:
                        return self.fields["announce"]

                    @property
                    def scrape(self) -> str:
                        return self.fields["scrape"]

                    @property
                    def tier(self) -> int:
                        return self.fields["tier"]
                """
                return [tracker.announce for tracker in (torrent.trackers or []) if tracker.tier >= 0 and tracker.announce]
        except Exception as e:
            print(str(e))
            return []

    def _get_label(self, torrent: Any, dl_type: str):
        """
        获取种子标签
        """
        try:
            return [str(tag).strip() for tag in torrent.get("tags", "").split(',')] \
                if dl_type == "qbittorrent" else torrent.labels or []
        except Exception as e:
            print(str(e))
            return []

    def _get_category(self, torrent: Any, dl_type: str):
        """
        获取种子分类
        """
        try:
            return torrent.get("category") if dl_type == "qbittorrent" else None
        except Exception as e:
            print(str(e))
            return None

    def _set_torrent_info(self, DOWNLOADER: str, _hash: str, _torrent: Any = None, _tags: list = [], _cat: str = None, _original_tags: list = None):
        """
        设置种子标签与分类
        """
        # 当前下载器
        downloader_obj = self._get_downloader(DOWNLOADER)
        if not _torrent:
            _torrent, error = downloader_obj.get_torrents(ids=_hash)
            if not _torrent or error:
                logger.error(
                        f"{self.LOG_TAG}设置种子标签与分类时发生了错误: 通过 {_hash} 查询不到任何种子!")
                return
            logger.info(
                    f"{self.LOG_TAG}设置种子标签与分类: {_hash} 查询到 {len(_torrent)} 个种子")
            _torrent = _torrent[0]
        # 判断是否可执行
        if DOWNLOADER and downloader_obj and _hash and _torrent:
            # 下载器api不通用, 因此需分开处理
            if DOWNLOADER == "qbittorrent":
                # 设置标签
                if _tags:
                    downloader_obj.set_torrents_tag(ids=_hash, tags=_tags)
                # 设置分类 <tr暂不支持>
                if _cat:
                    # 尝试设置种子分类, 如果失败, 则创建再设置一遍
                    try:
                        _torrent.setCategory(category=_cat)
                    except:
                        downloader_obj.qbc.torrents_createCategory(name=_cat)
                        _torrent.setCategory(category=_cat)
            else:
                # 设置标签
                if _tags:
                    # _original_tags = None表示未指定, 因此需要获取原始标签
                    if _original_tags == None:
                        _original_tags = self._get_label(torrent=_torrent, dl_type=DOWNLOADER)
                    # 如果原始标签不是空的, 那么合并原始标签
                    if _original_tags:
                        _tags = list(set(_original_tags).union(set(_tags)))
                    downloader_obj.set_torrent_tag(ids=_hash, tags=_tags)
            logger.warn(f"{self.LOG_TAG}下载器: {DOWNLOADER} 种子id: {_hash} {('  标签: ' + ','.join(_tags)) if _tags else ''} {('  分类: ' + _cat) if _cat else ''}")

    @eventmanager.register(EventType.DownloadAdded)
    def DownloadAdded(self, event: Event):
        """
        添加下载事件
        """
        if not self.get_state():
            return

        if not event.event_data:
            return
        
        try:
            context: Context = event.event_data.get("context")
            _hash = event.event_data.get("hash")
            _torrent = context.torrent_info
            _media = context.media_info
            _tags = []
            _cat = None
            # 站点标签, 如果勾选开关的话
            if self._enabled_tag and _torrent.site_name:
                _tags.append(_torrent.site_name)
            # 媒体标题标签, 如果勾选开关的话
            if self._enabled_media_tag and _media.title:
                _tags.append(_media.title)
            # 分类, 如果勾选开关的话 <tr暂不支持>
            if self._enabled_category and _media.type:
                _cat = self._genre_ids_get_cat(_media.type, _media.genre_ids)
            if _hash and (_tags or _cat):
                # 执行通用方法, 设置种子标签与分类
                self._set_torrent_info(DOWNLOADER=settings.DOWNLOADER, _hash=_hash, _tags=_tags, _cat=_cat)
        except Exception as e:
            logger.error(
                f"{self.LOG_TAG}分析下载事件时发生了错误: {str(e)}")


    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCheckboxBtn',
                                        'props': {
                                            'model': 'enabled_tag',
                                            'label': '自动站点标签',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCheckboxBtn',
                                        'props': {
                                            'model': 'enabled_media_tag',
                                            'label': '自动剧名标签',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VCheckboxBtn',
                                        'props': {
                                            'model': 'enabled_category',
                                            'label': '自动设置分类',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VCheckboxBtn',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '补全下载历史的标签与分类(一次性任务)'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'interval',
                                            'label': '定时任务',
                                            'items': [
                                                {'title': '禁用', 'value': '禁用'},
                                                {'title': '计划任务', 'value': '计划任务'},
                                                {'title': '固定间隔', 'value': '固定间隔'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_cron',
                                            'label': '计划任务设置',
                                            'placeholder': '5 4 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_time',
                                            'label': '固定间隔设置, 间隔每',
                                            'placeholder': '6'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'interval_unit',
                                            'label': '单位',
                                            'items': [
                                                {'title': '小时', 'value': '小时'},
                                                {'title': '分钟', 'value': '分钟'}
                                            ]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'category_movie',
                                            'label': '电影分类名称(默认: 电影)',
                                            'placeholder': '电影'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'category_tv',
                                            'label': '电视分类名称(默认: 电视)',
                                            'placeholder': '电视'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'category_anime',
                                            'label': '动漫分类名称(默认: 动漫)',
                                            'placeholder': '动漫'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '定时任务：支持两种定时方式，主要针对辅种刷流等种子补全站点信息。如没有对应的需求建议切换为禁用。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "enabled_tag": True,
            "enabled_media_tag": False,
            "enabled_category": False,
            "category_movie": "电影",
            "category_tv": "电视",
            "category_anime": "动漫",
            "interval": "计划任务",
            "interval_cron": "5 4 * * *",
            "interval_time": "6",
            "interval_unit": "小时"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
