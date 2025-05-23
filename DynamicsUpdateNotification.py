import json
from bilibili_api import user, sync, Credential
import time
import random
import requests

cre=Credential(sessdata=r"XXXXXXXX")
u = user.User(uid=3546831533378448,credential=cre)

async def getNewestDynamics():
    dynamics_ = []
    page = await u.get_dynamics_new()
    dynamics_.extend(page["items"])

    return dynamics_

knownTop=0
lastDynamics=0

async def pushMessage(message, debug=False):
    notification_url = "http://localhost:19218/send_group_msg"
    payload_Anti = \
    {
        "group_id": XXXXXXXX,
        "message":[{"type": "text","data": {"text": message}}]
    }
    payload_debug = \
    {
        "group_id": XXXXXXX,
        "message":[{"type": "text","data": {"text": message}}]
    }
    try:
        if debug:
            Rsp_debug = requests.post(notification_url, json=payload_debug, timeout=5)
        else:
            Rsp_anti = requests.post(notification_url, json=payload_Anti, timeout=5)
    except:
        time.sleep(5)
        if debug:
            Rsp_debug = requests.post(notification_url, json=payload_debug, timeout=5)
        else:
            Rsp_anti = requests.post(notification_url, json=payload_Anti, timeout=5)
    finally:
        return

        
dynamics_=sync(getNewestDynamics())
knownTop=dynamics_[0]["id_str"]
lastDynamics=dynamics_[1]["id_str"]
sync(pushMessage(f"功能已启用：埃瑟斯动态更新播报。\n目前埃瑟斯的置顶动态为https://t.bilibili.com/{knownTop} \n目前埃瑟斯的最新动态为https://t.bilibili.com/{lastDynamics}",True))

consecutive_exceptions_count = 0
last_exception = None

while True:
    try:
        dynamics_=sync(getNewestDynamics())
        if not dynamics_[0]["modules"].get("module_tag")=={"text":"置顶"}:
            sync(pushMessage("咦？埃瑟斯的置顶动态不见了？"))
        else:
            if not dynamics_[0]["id_str"]==knownTop:
                knownTop=dynamics_[0]["id_str"]
                sync(pushMessage(f"埃瑟斯更换了置顶动态！https://t.bilibili.com/{knownTop}"))
            elif not dynamics_[1]["id_str"]==lastDynamics:
                match dynamics_[1].get("type"):
                    case "DYNAMIC_TYPE_LIVE_RCMD":
                        sync(pushMessage("埃瑟斯发布了直播动态，不播报",True))
                    case "DYNAMIC_TYPE_AV":
                        sync(pushMessage("埃瑟斯发布了新视频（直播回放）！"))
                        lastDynamics=dynamics_[1]["id_str"]
                    case "DYNAMIC_TYPE_DRAW":
                        lastDynamics=dynamics_[1]["id_str"]
                        sync(pushMessage(f"埃瑟斯发布了新动态！https://t.bilibili.com/{lastDynamics}"))
                    case _:
                        lastDynamics=dynamics_[1]["id_str"]
                        sync(pushMessage(f"埃瑟斯大抵是发布了新动态...https://t.bilibili.com/{lastDynamics}"))
                        sync(pushMessage(f"没见过的动态type字符串!{dynamics_[1].get("type")}",True))
        consecutive_exceptions_count = 0
        last_exception = None
    except Exception as e:
        sync(pushMessage("动态更新播报发生异常",True))
        last_exception = e
        consecutive_exceptions_count += 1
        if consecutive_exceptions_count >= 3:
            sync(pushMessage("动态更新播报连续三次发生异常，正在退出。",True))
            print(f"Three consecutive exceptions. Quitting. Last exception was: {last_exception}")
            break
            
    time.sleep(120+random.randint(0,60))
