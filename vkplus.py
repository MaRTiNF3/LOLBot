# Standart library
import asyncio
import json
import random
import string

import aiohttp
import aiovk
import hues
from aiovk.drivers import HttpDriver
from aiovk.mixins import LimitRateDriverMixin
from captcha_solver import CaptchaSolver

from utils import fatal, MessageEventData, chunks

try:
    from settings import CAPTCHA_KEY, CAPTCHA_SERVER

    solver = CaptchaSolver(CAPTCHA_SERVER, api_key=CAPTCHA_KEY)
except (ImportError, AttributeError):
    solver = None


class NoPermissions(Exception):
    pass


# Драйвер для ограничения запросов к API - 3 раза в секунду
# На самом деле 3 запроса в 1.2 секунд (для уверенности)
class RatedDriver(LimitRateDriverMixin, HttpDriver):
    requests_per_period = 1
    period = 0.4

async def enter_captcha(url, sid):
    session = aiohttp.ClientSession()
    if not solver:
        return hues.error('Введите данные для сервиса решения капч в settings.py!')
    with session as ses:
        async with ses.get(url) as resp:
            img_data = await resp.read()
            data = solver.solve_captcha(img_data)
            # hues.success(f"Капча {sid} решена успешно")
            return data


class TokenSession(aiovk.TokenSession):
    async def enter_captcha(self, url, sid):
        return await enter_captcha(url, sid)

class ImplicitSession(aiovk.ImplicitSession):
    async def enter_captcha(self, url, sid):
        return await enter_captcha(url, sid)


# Словарь, ключ - раздел API методов, значение - список разрешённых методов
ALLOWED_METHODS = {
    'docs': ('getWallUploadServer', 'save'),

    'groups': ('getById',
               'getCallbackConfig',
               'getCallbackServer',
               'getCallbackSettings',
               'getMembers',
               'setCallbackServer',
               'setCallbackServerSettings',
               'setCallbackSettings'),

    'photos': ('getMessagesUploadServer',
               'saveMessagesPhoto')
}
# Словарь, ключ - раздел API методов, значение - список запрещённых методов
DISALLOWED_MESSAGES = ('addChatUser',
                       'allowMessagesFromGroup',
                       'createChat',
                       'denyMessagesFromGroup',
                       'deleteChatPhoto',
                       'editChat',
                       'getChat',
                       'getChatUsers',
                       'getLastActivity',
                       'markAsImportant',
                       'removeChatUser',
                       'searchDialogs',
                       'setActivity',
                       'setChatPhoto')


def is_available_from_group(key: str) -> bool:
    """Проверяет, можно ли выполнить данный метод VK API от имени группы"""
    # execute можно выполнять от имени группы
    if key == 'execute':
        return True
    try:
        topic, method = key.split('.')
    except ValueError:
        # Не должно случаться, но мало ли
        hues.warn('Метод VK API должен состоять из раздела и метода,'
                  ' разделённых точкой')
        return False
    # Если раздел - messages, проверяем, нельзя ли выполнить этот метод
    if topic == 'messages':
        return method not in DISALLOWED_MESSAGES
    # Получаем список разрешённых методов для данного раздела
    methods_allowed = ALLOWED_METHODS.get(topic, ())
    if method in methods_allowed:
        return True


# Методы, которые можно выполнять без авторизации API
ALLOWED_PUBLIC = {
    'apps': ('get', 'getCatalog'),

    'auth': ('checkPhone', 'confirm', 'restore', 'signup'),

    'board': ('getComments', 'getTopics'),

    'database': ('getChairs', 'getCities', 'getCitiesById',
                 'getCountries', 'getCountriesById', 'getFaculties',
                 'getRegions', 'getSchoolClasses', 'getSchools',
                 'getStreetsById', 'getUniversities'),

    'friends': ('get',),

    'groups': ('getById', 'getMembers', 'isMember'),

    'likes': ('getList',),

    'newsfeed': ('search',),

    'pages': ('clearCache',),

    'photos': ('get', 'getAlbums', 'getById', 'search'),

    'users': ('get', 'getFollowers', 'getSubscriptions'),

    'utils': ('checkLink', 'getServerTime', 'resolveScreenName'),

    'video': ('getCatalog', 'getCatalogSection'),

    'wall': ('get', 'getById', 'getComments', 'getReposts', 'search'),

    'widgets': ('getComments', 'getPages')
}


def is_available_from_public(key: str) -> bool:
    """Проверяет, доступен ли метод через паблик апи"""
    try:
        topic, method = key.split('.')
    except ValueError:
        # Не должно случаться, но мало ли
        hues.warn('Метод VK API должен состоять из раздела и метода,'
                  ' разделённых точкой')
        return False
    methods = ALLOWED_PUBLIC.get(topic, ())
    if method in methods:
        return True


class VkPlus(object):
    api = None

    def __init__(self, token=None, login=None, password=None, app_id=5668099, scope=140492191):
        # Методы, которые можно вызывать через токен сообщества
        self.group_methods = ('groups.getById', 'groups.getMembers', 'execute')

        self.token = token
        self.login = login
        self.password = password
        self.appid = app_id
        self.scope = scope
        self.init_vk()

    def init_vk(self):
        """Инициализация сессии ВК API"""
        if self.token:
            self.api_session = TokenSession(access_token=self.token, driver=RatedDriver())
        elif self.login and self.password:
            self.login = self.login
            self.password = self.password
            self.api_session = ImplicitSession(self.login, self.password, self.appid,
                                               scope=self.scope, driver=RatedDriver())  # all scopes
        else:
            fatal('Вы попытались инициализировать объект класса VkPlus без данных для авторизации!')
        self.api = aiovk.API(self.api_session)

        # Паблик API используется для методов, которые не нуждаются в регистрации (users.get и т.д)
        # Используется только при access_token сообщества вместо аккаунта
        if self.token:
            self.public_api_session = TokenSession(driver=RatedDriver())
            self.public_api = aiovk.API(self.public_api_session)

    async def method(self, key: str, data=None):
        """Выполняет метод API VK с дополнительными параметрами"""
        if data is None:
            data = {}
        # Если мы работаем от имени группы
        if self.token:
            # Если метод доступен от имени группы - используем API группы
            if is_available_from_group(key):
                api_method = self.api
            # Если метод доступен от паблик апи - используем его
            elif is_available_from_public(key):
                api_method = self.public_api

            else:
                hues.warn(f'Метод {key} нельзя вызвать от имени сообщества!')
                return {}
        else:
            api_method = self.api
        try:
            return await api_method(key, **data)
        except (asyncio.TimeoutError, json.decoder.JSONDecodeError):
            # Пытаемся отправить запрос к API ещё раз
            return await api_method(key, **data)
        except aiovk.exceptions.VkAuthError:
            message = 'TOKEN' if self.token else 'LOGIN и PASSWORD'
            fatal("Произошла ошибка при авторизации API, "
                  f"проверьте значение {message} в settings.py!")
        except aiovk.exceptions.VkAPIError as ex:
            # Код 9 - Flood error - слишком много одинаковых сообщений
            if not ex.error_code == 9:
                hues.error("Произошла ошибка при вызове метода API "
                           f"{key} с значениями {data}:\n{ex}")
                return {}

            if 'message' not in data:
                return {}

            data['message'] += f'\n Анти-флуд (API): {self.anti_flood()}'
            try:
                # Пытаемся отправить сообщение ещё раз
                await self.method('messages.send', data)
            except aiovk.exceptions.VkAPIError:
                # Не знаю, может ли это случиться, или нет
                hues.error('Обход анти-флуда API не удался =(')
        return {}

    @staticmethod
    def anti_flood():
        """Возвращает строку из 5 символов (букв и цифр)"""
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5))

    async def mark_as_read(self, message_ids):
        """Отмечает сообщение(я) как прочитанное(ые)"""
        await self.method('messages.markAsRead', {'message_ids': message_ids})

    async def resolve_name(self, screen_name):
        """Функция для перевода короткого имени в числовой ID"""
        try:
            for val in ('club', 'public', 'event'):
                screen_name = screen_name.replace(val, '')
            possible_id = int(screen_name)
            return possible_id

        except ValueError:
            result = await self.method('utils.resolveScreenName',
                                       {'screen_name': screen_name})
            if not result:
                return False

            return result.get('object_id')


class Message(object):
    """Класс, объект которого передаётся в плагин для упрощённого ответа"""
    __slots__ = ('_data', 'vk', 'conf', 'user', 'cid', 'id',
                 'body', 'timestamp', 'answer_values', 'attaches')

    def __init__(self, vk_api_object: VkPlus, data: MessageEventData):
        self._data = data
        self.vk = vk_api_object
        self.user = False
        # Если сообщение из конференции
        if data.conf:
            self.user = False
            self.cid = int(data.peer_id)
        else:
            self.user = True
        self.id = data.user_id
        self.body = data.body
        self.timestamp = data.time
        self.attaches = data.attaches
        # Словарь для отправки к ВК при ответе
        if self.user:
            self.answer_values = {'user_id': self.id}
        else:
            self.answer_values = {'chat_id': self.cid}

    async def answer(self, msg: str, **additional_values):
        """Функция ответа на сообщение для плагинов"""
        # Если длина сообщения больше 550 символов (получено эмпирическим путём)
        if len(msg) > 550:
            # Делим сообщение на список частей (каждая по 15 строк)
            msgs = list(chunks(msg.splitlines(), 15))
        else:
            # Иначе - создаём список из нашего сообщения
            msgs = [msg]
        if additional_values is None:
            additional_values = dict()
        # Отправляем каждое сообщение из списка
        for msg in msgs:
            data = msgs[0] if not len(msgs) > 1 else '\n'.join(msg)
            values = dict(**self.answer_values, message=data, **additional_values)
            await self.vk.method('messages.send', values)
