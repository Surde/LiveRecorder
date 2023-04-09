import asyncio
import json
import os
import time
from http.cookies import SimpleCookie
from subprocess import Popen
from typing import Dict, Tuple
from urllib import request

import ffmpeg
import httpx
from httpx_socks import AsyncProxyTransport
from jsonpath_ng.ext import parse
from loguru import logger
import streamlink
from streamlink.stream import StreamIO
from streamlink_cli.main import open_stream
from streamlink_cli.output import FileOutput
from streamlink_cli.streamrunner import StreamRunner

recording: Dict[str, Tuple[StreamIO, FileOutput, Popen]] = {}


class LiveRecoder:
    def __init__(self, config: dict, user: dict):
        self.proxy = config.get('proxy')

        self.id = user['id']
        platform = user['platform']
        name = user.get('name', self.id)
        self.flag = f'[{platform}][{name}]'

        self.interval = user.get('interval', 10)
        self.headers = user.get('headers', {'User-Agent': 'Chrome'})
        self.cookies = self.get_cookies(user.get('cookies', ''))

        self.client = self.get_client()

    async def start(self):
        logger.info(f'{self.flag}正在检测直播状态')
        while True:
            try:
                await self.run()
            except httpx.RequestError as error:
                logger.error(f'{self.flag}直播检测请求错误\n{repr(error)}')
            except Exception as error:
                logger.exception(f'{self.flag}直播检测未知错误\n{repr(error)}')
            await asyncio.sleep(self.interval)

    async def run(self):
        pass

    def get_client(self):
        kwargs = {
            'timeout': 10,
            'limits': httpx.Limits(max_keepalive_connections=100, keepalive_expiry=None),
            'headers': self.headers,
            'cookies': self.cookies
        }
        if self.proxy:
            if 'socks' in self.proxy:
                kwargs['transport'] = AsyncProxyTransport.from_url(self.proxy)
            else:
                kwargs['proxies'] = self.proxy
        else:
            self.proxy = request.getproxies().get('http')
        return httpx.AsyncClient(http2=True, **kwargs)

    @staticmethod
    def get_cookies(cookies_str: str):
        if cookies_str:
            cookies = SimpleCookie()
            cookies.load(cookies_str)
            return {k: v.value for k, v in cookies.items()}
        else:
            return {}

    def get_filename(self, title):
        live_time = time.strftime('%Y.%m.%d %H.%M.%S')
        # 文件名特殊字符转换为全角字符
        char_dict = {
            '"': '＂',
            '*': '＊',
            ':': '：',
            '<': '＜',
            '>': '＞',
            '?': '？',
            '/': '／',
            '\\': '＼',
            '|': '｜',
        }
        for half, full in char_dict.items():
            title = title.replace(half, full)
        filename = f'[{live_time}]{self.flag}{title}'
        return filename

    async def run_record(self, url, title):
        # 获取输出文件名
        filename = self.get_filename(title)

        logger.info(f'{self.flag}开始录制\n{url}\t{title}')
        # 新建output目录
        os.makedirs('output', exist_ok=True)
        # 创建ffmpeg管道
        pipe = self.create_pipe(filename)
        # 调用streamlink录制直播
        await asyncio.to_thread(self.stream_writer, url, title, pipe)  # 创建线程防止异步阻塞
        pipe.terminate()
        recording.pop(url, None)
        logger.info(f'{self.flag}停止录制\n{url}\t{title}')

    def create_pipe(self, filename):
        logger.info(f'{self.flag}创建ffmpeg管道')
        pipe = (
            ffmpeg
            .input('pipe:')
            .output(
                f'output/{filename}.mp4',
                loglevel='warning',
                codec='copy',
                map_metadata=-1,
            )
            .run_async(pipe_stdin=True)
        )
        return pipe

    def stream_writer(self, url, title, pipe):
        try:
            # Bilibili → HTTPStream[flv]
            # Youtube,Twitch → HLSStream[mpegts]
            # Twitcasting → Stream[mov,mp4,m4a,3gp,3g2,mj2]
            session = streamlink.Streamlink()
            # 添加streamlink的http相关选项
            for arg in ('proxy', 'headers', 'cookies'):
                if attr := getattr(self, arg):
                    session.set_option(f'http-{arg}', attr)
            # 添加Twitch跳过广告插件选项
            if 'twitch' in url:
                session.set_plugin_option('twitch', 'disable-ads', True)
            # stream为取最高清晰度的直播流，可能为空
            if stream := session.streams(url).get('best'):
                logger.info(f'{self.flag}获取到直播流链接\n{url}\t{title}\n{stream.url}')
                output = FileOutput(fd=pipe.stdin)
                stream_fd, prebuffer = open_stream(stream)
                try:
                    logger.info(f'{self.flag}正在录制\n{url}\t{title}')
                    output.open()
                    recording[url] = (stream_fd, output, pipe)
                    StreamRunner(stream_fd, output).run(prebuffer)
                except BrokenPipeError as error:
                    logger.exception(f'{self.flag}管道损坏错误\n{url}\t{title}\n{error}')
                except OSError as error:
                    logger.exception(f'{self.flag}文件写入错误\n{url}\t{title}\n{error}')
                finally:
                    output.close()
            else:
                logger.error(f'{self.flag}无可用直播源\n{url}\t{title}')
        except streamlink.StreamlinkError as error:
            logger.exception(f'{self.flag}streamlink错误\n{url}\t{title}\n{error}')
        except Exception as error:
            logger.exception(f'{self.flag}直播录制未知错误\n{url}\t{title}\n{error}')


class Bilibili(LiveRecoder):
    async def run(self):
        url = f'https://live.bilibili.com/{self.id}'
        if url not in recording:
            response = (await self.client.get(
                url='https://api.live.bilibili.com/room/v1/Room/get_info',
                params={'room_id': self.id}
            )).json()
            if response['data']['live_status'] == 1:
                title = response['data']['title']
                await self.run_record(url, title)


class Youtube(LiveRecoder):
    async def run(self):
        response = (await self.client.get(
            url=f'https://m.youtube.com/channel/{self.id}/streams',
            headers={
                'User-Agent': 'Android',
                'accept-language': 'zh-CN',
                'x-youtube-client-name': '2',
                'x-youtube-client-version': '2.20220101.00.00',
                'x-youtube-time-zone': 'Asia/Shanghai',
            },
            params={'pbj': 1}
        )).json()
        jsonpath = parse('$..videoWithContextRenderer').find(response)
        for item in [match.value for match in jsonpath]:
            if 'LIVE' in json.dumps(item):
                url = f"https://www.youtube.com/watch?v={item['videoId']}"
                title = item['headline']['runs'][0]['text']
                if url not in recording:
                    asyncio.create_task(self.run_record(url, title), name=url)


class Twitch(LiveRecoder):
    async def run(self):
        url = f'https://www.twitch.tv/{self.id}'
        if url not in recording:
            response = (await self.client.post(
                url='https://gql.twitch.tv/gql',
                headers={'Client-Id': 'kimne78kx3ncx6brgo4mv6wki5h1ko'},
                json=[{
                    'operationName': 'StreamMetadata',
                    'variables': {'channelLogin': self.id},
                    'extensions': {
                        'persistedQuery': {
                            'version': 1,
                            'sha256Hash': 'a647c2a13599e5991e175155f798ca7f1ecddde73f7f341f39009c14dbf59962'
                        }
                    }
                }]
            )).json()
            if response[0]['data']['user']['stream']:
                title = response[0]['data']['user']['lastBroadcast']['title']
                await self.run_record(url, title)


class Twitcasting(LiveRecoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client.headers['Origin'] = 'https://twitcasting.tv/'

    async def run(self):
        url = f'https://twitcasting.tv/{self.id}'
        if url not in recording:
            response = (await self.client.get(
                url=f'https://frontendapi.twitcasting.tv/users/{self.id}/latest-movie'
            )).json()
            if response['movie']['is_on_live']:
                movie_id = response['movie']['id']
                response = (await self.client.post(
                    url='https://twitcasting.tv/happytoken.php',
                    data={'movie_id': movie_id}
                )).json()
                token = response['token']
                response = (await self.client.get(
                    url=f'https://frontendapi.twitcasting.tv/movies/{movie_id}/status/viewer',
                    params={'token': token}
                )).json()
                title = response['movie']['title']
                await self.run_record(url, title)


async def run():
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    tasks = []
    for item in config['user']:
        platform_class = globals()[item['platform']]
        coro = platform_class(config, item).start()
        tasks.append(asyncio.create_task(coro))
    try:
        await asyncio.wait(tasks)
    except asyncio.CancelledError:
        logger.warning('用户中断录制，正在关闭直播流')
        for stream_fd, output, pipe in recording.copy().values():
            stream_fd.close()
            output.close()
            pipe.terminate()


if __name__ == '__main__':
    logger.add(
        sink='logs/log_{time:YYYY-MM-DD}.log',
        rotation='00:00',
        retention='3 days',
        level='INFO',
        encoding='utf-8',
        format='[{time:YYYY-MM-DD HH:mm:ss}][{level}][{name}][{function}:{line}]{message}'
    )
    asyncio.run(run())
