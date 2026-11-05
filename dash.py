#!/usr/bin/env python3
"""OSC 고려대 Workshop 01 — 고려대 LMS(Canvas) 데이터로 대시보드 한 장 만들기.

  python dash.py --list              # 화면 8가지 목록
  python dash.py --ui 1              # 내 LMS로 ① 화면 (환경변수 CANVAS_TOKEN 필요)
  python dash.py --ui 1 --sample     # 토큰 없이 가짜 데이터로
  python dash.py --ui 1 --save-data  # 받은 데이터도 data.json 으로 남기기

- 조회(GET)만 해요. LMS에 글을 쓰거나 지우는 요청은 하나도 없어요.
- 토큰은 화면에 찍지도, 파일에 저장하지도 않아요.
- 파이썬 기본 모듈만 써요(설치할 것 없음). Python 3.9 이상.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOST = os.environ.get("CANVAS_BASE_URL", "https://mylms.korea.ac.kr").rstrip("/")
API = HOST + "/api/v1"
KST = timezone(timedelta(hours=9))

# 번호: (영문 이름, 화면 이름, 필요한 데이터)
UIS = {
    1: ("missing", "안 낸 것만 모은 폰 목록", {"courses", "tasks"}),
    2: ("calendar", "달력 칸에 들어간 마감", {"courses", "tasks", "events"}),
    3: ("briefing", "대화로 오는 아침 브리핑", {"courses", "tasks", "announcements", "unread"}),
    4: ("week", "이번 주 낸 비율 막대", {"courses", "tasks"}),
    5: ("materials", "주차별 자료 체크리스트", {"courses", "modules"}),
    6: ("scores", "과목별 점수 카드", {"courses", "assignments"}),
    7: ("news", "과목별 새 공지 묶음", {"courses", "announcements", "unread"}),
    8: ("quest", "게임처럼 키우는 과제", {"courses", "tasks", "assignments"}),
}


# ───────────────────────── 시간 ─────────────────────────

def now_kst() -> datetime:
    return datetime.now(KST).replace(microsecond=0)


def to_kst(iso: str | None) -> str | None:
    """Canvas 의 UTC 시각 문자열 → 한국 시각 ISO 문자열."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST).isoformat()
    except ValueError:
        return None


def parse(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso) if iso else None


# ───────────────────────── LMS 요청 ─────────────────────────

class LmsError(Exception):
    pass


class Lms:
    def __init__(self, token: str):
        self._headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _open(self, url: str):
        req = urllib.request.Request(url, headers=self._headers, method="GET")
        try:
            return urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise LmsError("401 — 토큰이 틀렸거나 만료됐어요. LMS에서 토큰을 다시 복사해요.") from None
            if e.code == 403:
                raise LmsError(f"403 — 이 자료는 볼 권한이 없어요 ({url.split('?')[0].replace(API, '')}).") from None
            raise LmsError(f"HTTP {e.code} — {url.split('?')[0].replace(API, '')}") from None
        except urllib.error.URLError as e:
            raise LmsError(f"LMS에 연결하지 못했어요 ({e.reason}). 인터넷이 막혀 있으면 --sample 로 해 봐요.") from None

    def get(self, path: str, params: list[tuple] | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        with self._open(url) as r:
            body = r.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise LmsError("JSON 대신 웹 페이지가 왔어요. 주소가 mylms.korea.ac.kr 인지 확인해요.") from None

    def get_all(self, path: str, params: list[tuple] | None = None, max_pages: int = 10) -> list:
        """Link 헤더의 rel="next" 를 따라가며 모든 페이지를 모아요."""
        params = list(params or [])
        if not any(k == "per_page" for k, _ in params):
            params.append(("per_page", 100))
        url: str | None = API + path + "?" + urllib.parse.urlencode(params)
        out: list = []
        for _ in range(max_pages):
            if not url:
                break
            with self._open(url) as r:
                link = r.headers.get("Link", "")
                body = r.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                raise LmsError("JSON 대신 웹 페이지가 왔어요. 주소가 mylms.korea.ac.kr 인지 확인해요.") from None
            out.extend(data if isinstance(data, list) else [data])
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None
        return out


# ───────────────────────── 정리 도우미 ─────────────────────────

PREFIX_RE = re.compile(r"^\s*\d{3}[A-Z]?\s*\([^)]*\)\s*")   # "262R (서울-학부)"
BRACKET_RE = re.compile(r"^\s*(\[[^\]]*\]\s*)+")            # "[학생] [한국어]"


def short_course_name(name: str) -> str:
    """'262R (서울-학부)공학수학Ⅱ(영강)(ENGINEERING …)-03분반' → '공학수학Ⅱ'."""
    n = PREFIX_RE.sub("", name or "")
    n = BRACKET_RE.sub("", n)
    head = n.split("(")[0].strip()
    if head:
        n = head
    n = re.sub(r"\s*-\s*\d+분반\s*$", "", n).strip(" -")
    return n or name


def plain_text(h: str | None, limit: int = 400) -> str:
    """공지 본문 HTML → 글자만, 앞부분만."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def absolute(url: str | None) -> str | None:
    if not url:
        return None
    return url if url.startswith("http") else HOST + url


def task_status(sub: dict | None, due_at: str | None, now: datetime) -> str:
    """플래너 제출 정보 → todo / submitted / late / missing / graded / excused."""
    sub = sub if isinstance(sub, dict) else {}
    if sub.get("excused"):
        return "excused"
    if sub.get("graded"):
        return "graded"
    if sub.get("submitted"):
        return "late" if sub.get("late") else "submitted"
    due = parse(due_at)
    if sub.get("missing") or (due and due < now):
        return "missing"
    return "todo"


KIND = {"assignment": "assignment", "quiz": "quiz", "discussion_topic": "discussion",
        "File": "file", "Page": "page", "ExternalUrl": "link", "ExternalTool": "link",
        "Assignment": "assignment", "Quiz": "quiz", "Discussion": "discussion", "SubHeader": "header"}


# ───────────────────────── 데이터 받기 ─────────────────────────

def fetch(needs: set[str], token: str, now: datetime) -> dict:
    lms = Lms(token)
    data: dict = {"generated_at": now.isoformat(), "source": "lms", "host": HOST}

    raw = lms.get_all("/users/self/courses", [("enrollment_state", "active"),
                                               ("include[]", "total_scores"), ("include[]", "term")])
    courses = []
    for c in raw:
        if not c.get("name"):
            continue
        enr = next((e for e in c.get("enrollments") or [] if e.get("type") == "student"), {})
        courses.append({"id": c["id"], "name": short_course_name(c["name"]), "full_name": c["name"],
                        "score": enr.get("computed_current_score"), "grade": enr.get("computed_current_grade"),
                        "url": f"{HOST}/courses/{c['id']}"})
    data["courses"] = courses
    names = {c["id"]: c["name"] for c in courses}
    codes = [f"course_{c['id']}" for c in courses]

    if "tasks" in needs:
        month_start = now.replace(day=1, hour=0, minute=0, second=0)
        start = min(now - timedelta(days=7), month_start)
        end = (month_start + timedelta(days=32)).replace(day=1) + timedelta(days=7)
        end = max(end, now + timedelta(days=35))
        items = lms.get_all("/planner/items", [("start_date", start.isoformat()), ("end_date", end.isoformat())])
        tasks, events = [], []
        for p in items:
            kind = p.get("plannable_type")
            pl = p.get("plannable") or {}
            cid = p.get("course_id")
            course = names.get(cid) or short_course_name(p.get("context_name") or "")
            if kind == "calendar_event":
                events.append({"course_id": cid, "course": course, "title": pl.get("title") or "",
                               "start_at": to_kst(pl.get("start_at") or p.get("plannable_date")),
                               "end_at": to_kst(pl.get("end_at")), "all_day": bool(pl.get("all_day")),
                               "url": absolute(p.get("html_url"))})
                continue
            if kind not in ("assignment", "quiz", "discussion_topic"):
                continue
            due = to_kst(pl.get("due_at") or p.get("plannable_date"))
            sub = p.get("submissions")
            tasks.append({"id": f"{kind}_{p.get('plannable_id')}", "course_id": cid, "course": course,
                          "title": pl.get("title") or pl.get("name") or "", "kind": KIND.get(kind, kind),
                          "due_at": due, "url": absolute(p.get("html_url")),
                          "points": pl.get("points_possible"), "status": task_status(sub, due, now)})
        # 플래너 기간 밖에서 이미 마감이 지난 미제출 과제도 붙여요.
        seen = {t["id"] for t in tasks}
        for a in lms.get_all("/users/self/missing_submissions", [("filter[]", "submittable")]):
            tid = f"assignment_{a.get('id')}"
            if tid in seen:
                continue
            due = to_kst(a.get("due_at"))
            tasks.append({"id": tid, "course_id": a.get("course_id"), "course": names.get(a.get("course_id"), ""),
                          "title": a.get("name") or "", "kind": "assignment", "due_at": due,
                          "url": absolute(a.get("html_url")), "points": a.get("points_possible"), "status": "missing"})
        tasks.sort(key=lambda t: t["due_at"] or "9999")
        data["tasks"] = tasks
        data["events"] = events

    if "events" in needs:
        start = now.replace(day=1, hour=0, minute=0, second=0)
        end = (start + timedelta(days=32)).replace(day=1) + timedelta(days=7)
        evs = data.get("events", [])
        seen = {(e["title"], e["start_at"]) for e in evs}
        for i in range(0, len(codes), 10):   # context_codes 는 한 번에 10개까지
            params = [("context_codes[]", c) for c in codes[i:i + 10]]
            params += [("type", "event"), ("start_date", start.date().isoformat()), ("end_date", end.date().isoformat())]
            for e in lms.get_all("/calendar_events", params, max_pages=3):
                cid = int(str(e.get("context_code", "_0")).split("_")[-1] or 0)
                item = {"course_id": cid, "course": names.get(cid, ""), "title": e.get("title") or "",
                        "start_at": to_kst(e.get("start_at")), "end_at": to_kst(e.get("end_at")),
                        "all_day": bool(e.get("all_day")), "url": absolute(e.get("html_url"))}
                if (item["title"], item["start_at"]) not in seen:
                    evs.append(item)
        data["events"] = sorted(evs, key=lambda e: e["start_at"] or "")

    if "announcements" in needs:
        anns = []
        for i in range(0, len(codes), 10):
            params = [("context_codes[]", c) for c in codes[i:i + 10]]
            params += [("start_date", (now - timedelta(days=14)).date().isoformat()),
                       ("end_date", (now + timedelta(days=1)).date().isoformat())]
            for a in lms.get_all("/announcements", params, max_pages=3):
                cid = int(str(a.get("context_code", "_0")).split("_")[-1] or 0)
                anns.append({"id": a.get("id"), "course_id": cid, "course": names.get(cid, ""),
                             "title": a.get("title") or "", "posted_at": to_kst(a.get("posted_at")),
                             "text": plain_text(a.get("message")), "url": absolute(a.get("html_url")),
                             "unread": a.get("read_state") == "unread"})
        data["announcements"] = sorted(anns, key=lambda a: a["posted_at"] or "", reverse=True)

    if "unread" in needs:
        try:
            data["unread_messages"] = int(lms.get("/conversations/unread_count").get("unread_count"))
        except (LmsError, TypeError, ValueError):
            data["unread_messages"] = None

    if "modules" in needs:
        mods = []
        for c in courses:
            try:
                raw_mods = lms.get_all(f"/courses/{c['id']}/modules", [("include[]", "items")], max_pages=3)
            except LmsError:
                continue   # 모듈 탭을 닫아 둔 과목
            for m in raw_mods:
                items = m.get("items")
                if items is None and m.get("items_url"):
                    try:
                        items = lms.get_all(m["items_url"].replace(API, ""), max_pages=2)
                    except LmsError:
                        items = []
                mods.append({"course_id": c["id"], "course": c["name"], "name": m.get("name") or "",
                             "position": m.get("position"),
                             "items": [{"id": it.get("id"), "title": it.get("title") or "",
                                        "kind": KIND.get(it.get("type"), "other"),
                                        "url": absolute(it.get("html_url")),
                                        "done": (it.get("completion_requirement") or {}).get("completed")}
                                       for it in items or []]})
        data["modules"] = mods

    if "assignments" in needs:
        rows = []
        for c in courses:
            try:
                raw_as = lms.get_all(f"/courses/{c['id']}/assignments", [("include[]", "submission")], max_pages=3)
            except LmsError:
                continue
            for a in raw_as:
                s = a.get("submission") or {}
                rows.append({"id": a.get("id"), "course_id": c["id"], "course": c["name"],
                             "title": a.get("name") or "", "due_at": to_kst(a.get("due_at")),
                             "points": a.get("points_possible"), "submitted_at": to_kst(s.get("submitted_at")),
                             "late": bool(s.get("late")), "missing": bool(s.get("missing")),
                             "score": s.get("score"), "graded_at": to_kst(s.get("graded_at")),
                             "url": absolute(a.get("html_url"))})
        data["assignments"] = sorted(rows, key=lambda a: a["due_at"] or "9999")

    return data


# ───────────────────────── 가짜 데이터 ─────────────────────────

def sample(now: datetime) -> dict:
    """토큰 없이 해 볼 때 쓰는 가짜 학기. 시각은 '지금' 기준이라 언제 돌려도 그럴듯해요."""
    def at(days: float, hour: int = 23, minute: int = 59) -> str:
        d = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0)
        return d.isoformat()

    C = [(101, "공학수학Ⅱ", 88.4, "A"), (102, "전자기학", 79.0, "B+"), (103, "데이터구조와알고리즘", 93.2, "A+"),
         (104, "디지털시스템실험", None, None), (105, "글쓰기", 85.0, "A-")]
    courses = [{"id": i, "name": n, "full_name": n, "score": s, "grade": g, "url": f"{HOST}/courses/{i}"}
               for i, n, s, g in C]
    nm = {i: n for i, n, *_ in C}

    def task(i, cid, title, kind, days, hour, status, pts=10):
        return {"id": f"{kind}_{i}", "course_id": cid, "course": nm[cid], "title": title, "kind": kind,
                "due_at": at(days, hour), "url": f"{HOST}/courses/{cid}/assignments/{i}", "points": pts,
                "status": status}

    tasks = [
        task(1, 102, "과제 5 — 맥스웰 방정식", "assignment", -2, 23, "missing", 20),
        task(2, 101, "Lec 14 복습 퀴즈", "quiz", -1, 18, "graded", 5),
        task(3, 103, "실습 8 — 힙 정렬", "assignment", -1, 23, "late", 10),
        task(4, 104, "8주차 결과보고서", "assignment", 0, 23, "todo", 30),
        task(5, 101, "HW 7 — 라플라스 변환", "assignment", 1, 9, "todo", 20),
        task(6, 105, "에세이 초안 토론", "discussion", 2, 18, "submitted", 10),
        task(7, 103, "실습 9 — 그래프 탐색", "assignment", 3, 23, "todo", 10),
        task(8, 102, "Lec 16 퀴즈", "quiz", 4, 12, "todo", 5),
        task(9, 105, "에세이 최종본", "assignment", 6, 23, "todo", 40),
        task(10, 104, "9주차 예비보고서", "assignment", 8, 9, "todo", 20),
        task(11, 101, "HW 8 — 푸리에 급수", "assignment", 12, 23, "todo", 20),
        task(12, 103, "기말 프로젝트 제안서", "assignment", 17, 23, "todo", 30),
    ]
    events = [
        {"course_id": 102, "course": "전자기학", "title": "중간고사 성적 열람", "start_at": at(5, 14, 0),
         "end_at": at(5, 15, 0), "all_day": False, "url": None},
        {"course_id": 103, "course": "데이터구조와알고리즘", "title": "보강 (온라인)", "start_at": at(9, 19, 0),
         "end_at": at(9, 20, 30), "all_day": False, "url": None},
    ]
    anns = [
        (102, "과제 5 마감 연장 안내", 0.2, True,
         "과제 5 마감을 금요일 밤 11시 59분으로 미룹니다. 3번 문제의 경계 조건 오타를 고쳐 다시 올렸으니 새 파일로 풀어 주세요."),
        (103, "실습 9 테스트 케이스 공개", 0.6, True,
         "실습 9 채점에 쓰는 공개 테스트 케이스 12개를 올렸습니다. 숨은 케이스는 8개이고, 시간 제한은 케이스당 1초입니다."),
        (101, "다음 주 수업은 녹화 강의로 대체", 1.5, False,
         "다음 주 화요일 수업은 학회 일정으로 녹화 강의로 대체합니다. 강의 영상은 월요일 저녁에 올라오고 출석은 시청 기록으로 확인합니다."),
        (105, "에세이 피드백 면담 신청", 3, False,
         "에세이 초안 피드백 면담을 신청받습니다. 시간표 링크에서 15분 칸을 골라 주세요. 면담은 다음 주 수요일까지입니다."),
        (104, "실험실 장비 점검으로 9주차 장소 변경", 5, False,
         "9주차 실험은 장비 점검으로 공학관 4층 실습실에서 진행합니다. 예비보고서 제출 마감은 그대로입니다."),
    ]
    announcements = [{"id": 900 + k, "course_id": cid, "course": nm[cid], "title": t,
                      "posted_at": (now - timedelta(days=d)).isoformat(), "text": body,
                      "url": f"{HOST}/courses/{cid}/discussion_topics/{900 + k}", "unread": u}
                     for k, (cid, t, d, u, body) in enumerate(anns)]

    def mod(cid, week, files, done=0):
        return {"course_id": cid, "course": nm[cid], "name": f"{week}주차", "position": week,
                "items": [{"id": cid * 1000 + week * 10 + j, "title": f, "kind": k,
                           "url": f"{HOST}/courses/{cid}/modules/items/{cid * 1000 + week * 10 + j}",
                           "done": (j < done) if k != "header" else None}
                          for j, (k, f) in enumerate(files)]}

    modules = [
        mod(101, 9, [("file", "Lec15_복소함수.pdf"), ("file", "Lec16_코시정리.pdf"), ("assignment", "HW 7")], 1),
        mod(101, 10, [("file", "Lec17_유수정리.pdf"), ("page", "중간 이후 시험 범위 안내")], 0),
        mod(102, 9, [("file", "Ch7_전자기유도.pdf"), ("link", "강의 녹화 (Zoom)"), ("quiz", "Lec 16 퀴즈")], 2),
        mod(103, 9, [("file", "09_그래프.pdf"), ("file", "실습9_명세.pdf"), ("assignment", "실습 9")], 1),
        mod(104, 9, [("header", "9주차 — 카운터와 FSM"), ("file", "9주차_실험지침서.pdf"), ("file", "예비보고서_양식.docx")], 0),
        mod(105, 9, [("page", "에세이 평가 기준"), ("discussion", "에세이 초안 토론")], 2),
    ]

    def done_row(i, cid, title, days, pts, score, late=False, graded=True):
        due = at(days, 23)
        sub = (datetime.fromisoformat(due) - timedelta(hours=2 if not late else -20)).isoformat()
        return {"id": i, "course_id": cid, "course": nm[cid], "title": title, "due_at": due, "points": pts,
                "submitted_at": sub, "late": late, "missing": False, "score": score if graded else None,
                "graded_at": (datetime.fromisoformat(due) + timedelta(days=3)).isoformat() if graded else None,
                "url": f"{HOST}/courses/{cid}/assignments/{i}"}

    assignments = [
        done_row(201, 101, "HW 1", -60, 20, 18), done_row(202, 101, "HW 3", -45, 20, 16),
        done_row(203, 101, "HW 5", -25, 20, 19), done_row(204, 101, "HW 6", -10, 20, 17),
        done_row(211, 102, "과제 2", -50, 20, 14), done_row(212, 102, "과제 3", -35, 20, 15, late=True),
        done_row(213, 102, "과제 4", -18, 20, 17),
        done_row(221, 103, "실습 5", -30, 10, 10), done_row(222, 103, "실습 6", -20, 10, 9),
        done_row(223, 103, "실습 7", -9, 10, 10), done_row(224, 103, "실습 8", -1, 10, None, late=True, graded=False),
        done_row(231, 105, "에세이 개요", -28, 10, 8.5), done_row(232, 105, "에세이 초안 토론", 2, 10, None, graded=False),
    ]
    assignments.append({"id": 214, "course_id": 102, "course": "전자기학", "title": "과제 5", "due_at": at(-2, 23),
                        "points": 20, "submitted_at": None, "late": False, "missing": True, "score": None,
                        "graded_at": None, "url": f"{HOST}/courses/102/assignments/214"})
    return {"generated_at": now.isoformat(), "source": "sample", "host": HOST, "courses": courses,
            "tasks": tasks, "events": events, "announcements": announcements, "unread_messages": 4,
            "modules": modules, "assignments": sorted(assignments, key=lambda a: a["due_at"] or "9999")}


# ───────────────────────── 화면 그리기 ─────────────────────────
# render_<영문 이름>(data, now) 가 HTML 한 장(문자열)을 돌려줘요. ③ briefing 만 대화용 글(마크다운)이에요.
# data 의 모양은 sample() 이 만드는 것과 같아요. 시각은 모두 한국 시각 ISO 문자열이에요.

# ==== VIEWS START ====

def _e(value):
    return html.escape('' if value is None else str(value), quote=True)


def _items(data, key):
    value = data.get(key) if isinstance(data, dict) else None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _dt(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=KST) if stamp.tzinfo is None else stamp.astimezone(KST)
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value, default=None):
    try:
        number = float(value)
        return number if number == number and abs(number) != float('inf') else default
    except (TypeError, ValueError, OverflowError):
        return default


def _date(value, time=True):
    stamp = _dt(value)
    if stamp is None:
        return '날짜 미정'
    label = f"{stamp.month}/{stamp.day}({'월화수목금토일'[stamp.weekday()]})"
    return label + (f' {stamp:%H:%M}' if time else '')


def _due_label(value, now):
    due = _dt(value)
    if due is None:
        return '마감 미정'
    now = _dt(now)
    seconds = (due - now).total_seconds()
    days = (due.date() - now.date()).days
    if seconds < 0:
        elapsed = (now.date() - due.date()).days
        return f'{elapsed}일 지남' if elapsed else '오늘 마감 지남'
    if seconds < 3600:
        return f'{max(1, int(seconds // 60))}분 남음'
    if seconds < 6 * 3600:
        return f'{max(1, int(seconds // 3600))}시간 남음'
    return '오늘' if days == 0 else f'D-{days}'


def _link(item, label=None, cls=''):
    title = _e(label if label is not None else item.get('title') or '제목 없는 항목')
    url = str(item.get('url') or '').strip()
    try:
        safe = urllib.parse.urlsplit(url).scheme.lower() in ('http', 'https')
    except ValueError:
        safe = False
    if not safe:
        return f'<span class="{_e(cls)}">{title}</span>'
    return f'<a class="{_e(cls)}" href="{_e(url)}" target="_blank" rel="noopener">{title}</a>'


def _course(item):
    return _e(item.get('course') or '과목 정보 없음')


def _done(item):
    return item.get('status') in ('submitted', 'graded', 'late', 'excused')


def _status(item):
    labels = {'missing': ('안 냄', 'bad'), 'late': ('지각', 'good'), 'submitted': ('냄', 'good'),
              'graded': ('채점됨', 'good'), 'excused': ('면제', 'good'), 'todo': ('낼 것', 'neutral')}
    label, style = labels.get(item.get('status'), ('확인 필요', 'neutral'))
    return f'<span class="pill {style}">{label}</span>'


def _task_row(item, now):
    return (f'<li class="task-row"><div class="course">{_course(item)}</div>'
            f'<div class="task-title">{_link(item)}</div><div class="meta">'
            f'<span>{_e(_date(item.get("due_at")))}</span><span class="due-tag">{_e(_due_label(item.get("due_at"), now))}</span>{_status(item)}</div></li>')


def _empty(message):
    return f'<div class="empty"><span class="empty-mark" aria-hidden="true">—</span><p>{_e(message)}</p></div>'


def _section(title, content):
    return f'<section class="section"><h2 class="section-head">{_e(title)}</h2>{content}</section>'


_VIEW_CSS = r'''
:root{color-scheme:light dark;--bg:#F5F4F1;--paper:#fff;--ink:#181724;--muted:#696675;--line:#E5E3E9;--crimson:#8B0029;--green:#0E7C47;--blue:#1463F3;--rose:#F6E4EA;--mint:#E2F3E8;--sky:#E3ECFE;--shadow:0 4px 20px #18172404}
@media(prefers-color-scheme:dark){:root{--bg:#121219;--paper:#1E1E28;--ink:#F1EFF7;--muted:#B0ADBC;--line:#373440;--crimson:#FF9FB9;--green:#83DBAA;--blue:#9AB9FF;--rose:#40202C;--mint:#16372A;--sky:#202F4C;--shadow:none}}
*{box-sizing:border-box}html{font-family:"Pretendard","Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",system-ui,sans-serif;font-size:14px;line-height:1.65;-webkit-text-size-adjust:100%;background:var(--bg);color:var(--ink)}
body{margin:0}main{max-width:760px;margin:auto;padding:32px 20px 26px;min-width:0}main.wide{max-width:1080px}a{color:inherit;text-decoration:none;overflow-wrap:anywhere}a:hover{text-decoration:underline;text-underline-offset:3px}
a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible{outline:3px solid var(--blue);outline-offset:3px}button,input,summary,label{font:inherit}button,summary{cursor:pointer}button{color:var(--ink);background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:10px 15px;min-height:44px}input[type=checkbox]{accent-color:var(--green)}
.page-head{margin-bottom:26px}.brandline{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.wordmark{font-size:11px;font-weight:800;letter-spacing:1.5px;color:var(--crimson);display:flex;align-items:center;gap:9px}.wordmark:before{content:"";width:9px;height:9px;background:var(--crimson);border-radius:2px;display:block}
.sample-badge{border:1px solid var(--crimson);color:var(--crimson);background:var(--rose);padding:3px 9px;border-radius:6px;font-size:11px;font-weight:700;white-space:nowrap}h1{font-size:27px;line-height:1.35;letter-spacing:-1.2px;margin:0 0 8px;font-weight:800;word-break:keep-all;overflow-wrap:anywhere}.head-date{font-size:12px;color:var(--muted);margin:0}
.section{margin:26px 0}.section-head{font-size:15px;font-weight:800;letter-spacing:-.3px;margin:0 0 11px;display:flex;gap:8px;align-items:center}.eyebrow,.course{font-size:11px;font-weight:700;letter-spacing:.2px;color:var(--muted)}.eyebrow{letter-spacing:1px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow);min-width:0}.muted{color:var(--muted)}.metric{font-size:48px;line-height:1.15;font-weight:800;letter-spacing:-2px;font-variant-numeric:tabular-nums}
.task-list{list-style:none;padding:0;margin:0;border:1px solid var(--line);background:var(--paper);border-radius:16px;overflow:hidden}.task-row{padding:15px 17px;min-width:0}.task-row+.task-row{border-top:1px solid var(--line)}.task-title{font-size:15px;font-weight:700;line-height:1.5;overflow-wrap:anywhere;word-break:keep-all}.task-title a{display:flex;align-items:center;min-height:40px;padding:4px 0}.task-title>span{display:block;padding:6px 0}
.task-title a{min-width:40px}
.meta{display:flex;align-items:center;gap:6px 9px;flex-wrap:wrap;color:var(--muted);font-size:11px}.due-tag{font-weight:700;color:var(--ink)}.pill{display:inline-flex;align-items:center;gap:4px;font-size:10px;line-height:1.5;font-weight:700;padding:3px 8px;border-radius:6px;white-space:nowrap}.bad{background:var(--rose);color:var(--crimson)}.good{background:var(--mint);color:var(--green)}.neutral{background:var(--sky);color:var(--blue)}
.empty{padding:25px 18px;text-align:center;background:var(--paper);border:1px dashed var(--line);border-radius:16px;color:var(--muted)}.empty p{margin:3px 0;font-size:13px}.empty-mark{font-size:25px;color:var(--green)}.progress{height:9px;border-radius:99px;background:var(--line);overflow:hidden}.progress>span{display:block;height:100%;border-radius:inherit;background:var(--green)}
details{min-width:0}summary{min-height:44px;padding:12px 0;font-weight:700;overflow-wrap:anywhere}summary::marker{color:var(--muted)}svg{max-width:100%}p{overflow-wrap:anywhere}.page-foot{border-top:1px solid var(--line);margin-top:32px;padding-top:16px;color:var(--muted);font-size:10px;line-height:1.9;overflow-wrap:anywhere}.page-foot p{margin:0}noscript{display:block;color:var(--muted);font-size:11px;margin-top:10px}
@media(min-width:700px){main{padding-top:48px;padding-bottom:40px}h1{font-size:32px}.page-head{margin-bottom:30px}.task-row{padding:17px 20px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
'''


_GLASS_CREDIT = '''<!-- Glass edge technique inspired by liquid-glass-js (MIT, Copyright (c) 2025 Armagan Amcalar).
Original compact CSS/SVG implementation; no library or screenshot capture code is bundled.
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions: The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
-->
'''

_GLASS_SVG = '''<svg class="glass-defs" width="0" height="0" aria-hidden="true" focusable="false"><defs><filter id="osc-glass-edge" x="-10%" y="-10%" width="120%" height="120%" color-interpolation-filters="sRGB"><feTurbulence type="fractalNoise" baseFrequency=".012 .035" numOctaves="1" seed="8" result="edgeNoise"/><feDisplacementMap in="SourceGraphic" in2="edgeNoise" scale="5" xChannelSelector="R" yChannelSelector="G"/></filter></defs></svg>'''

_POLISH_CSS = r'''
:root{--bg:#f6ece4;--paper:rgba(255,251,247,.92);--ink:#2c171d;--muted:#674b53;--line:rgba(92,47,58,.16);--crimson:#85002a;--green:#285a43;--blue:#64354f;--rose:#f1dce0;--mint:#e2ebe1;--sky:#eee2e9;--shadow:0 14px 40px -24px rgba(70,11,33,.30),inset 0 1px 0 rgba(255,255,255,.95);--rim:rgba(255,255,255,.88);--glass-solid:#fff8f2}
@media(prefers-color-scheme:dark){:root{--bg:#190b13;--paper:rgba(35,18,28,.94);--ink:#fff3eb;--muted:#d9bec4;--line:rgba(240,204,202,.20);--crimson:#f2b2bf;--green:#b3dfc1;--blue:#e3c2da;--rose:#48212f;--mint:#223b2c;--sky:#3b2839;--shadow:0 18px 46px -20px #05010688,inset 0 1px 0 #f8dac326;--rim:rgba(255,234,217,.34);--glass-solid:#24121d}}
html{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;line-height:1.7;background:var(--bg);color:var(--ink)}
body{min-width:0;background:transparent}main{padding:28px 20px 32px;max-width:800px;position:relative}main.wide{max-width:1160px}.glass-defs{position:absolute;overflow:hidden;pointer-events:none}
.page-head{margin-bottom:28px}.brandline{margin-bottom:26px;gap:8px}.wordmark{font-size:10px;letter-spacing:1.3px;font-weight:750;color:var(--crimson)}.wordmark:before{width:9px;height:9px;border-radius:50%;box-shadow:0 0 0 4px var(--rose);margin:0 3px}
.sample-badge{color:var(--muted);background:var(--paper);border:1px solid var(--line);border-radius:99px;font-size:10px;font-weight:600;padding:3px 9px}
h1{font-size:28px;line-height:1.32;letter-spacing:-1.4px;font-weight:750;margin:0 0 12px;max-width:680px}.head-date{font-size:12px;letter-spacing:-.15px;color:var(--muted)}
.section{margin:29px 0}.section-head{font-size:15px;font-weight:750;letter-spacing:-.35px;margin-bottom:14px}.eyebrow,.course{font-size:11px;font-weight:650;letter-spacing:.15px;color:var(--muted)}.eyebrow{letter-spacing:.4px}.course{line-height:1.6}
.card,.task-list,.missing-hero,.empty,.sq-results,.sq-quest-list{position:relative;isolation:isolate;background:var(--paper);border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow);-webkit-backdrop-filter:blur(18px) saturate(1.08);backdrop-filter:blur(18px) saturate(1.08)}
.card:before,.task-list:before,.missing-hero:before,.sq-results:before,.sq-quest-list:before{content:"";position:absolute;inset:0;padding:2px;border-radius:inherit;pointer-events:none;z-index:2;background:linear-gradient(135deg,var(--rim),transparent 26%,transparent 65%,var(--rim));-webkit-mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);-webkit-mask-composite:xor;mask:linear-gradient(#fff 0 0) content-box,linear-gradient(#fff 0 0);mask-composite:exclude;-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px) url(#osc-glass-edge)}
.card{padding:24px}.task-list{overflow:hidden}.task-row{padding:18px 20px}.task-row+.task-row{border-top:1px solid var(--line)}.task-title{font-size:14px;font-weight:650;letter-spacing:-.25px;line-height:1.6}.task-title a{padding:4px 0;min-height:44px}.meta{font-size:11px;gap:7px 10px;line-height:1.6}.due-tag{font-weight:650}
.pill{font-size:10px;font-weight:650;letter-spacing:0;padding:4px 8px;border-radius:7px}.empty{border-style:dashed;padding:28px 20px}.empty p{font-size:13px}.hero-note{opacity:1}.page-foot{font-size:10px;line-height:1.9;margin-top:34px;border-color:var(--line);padding:18px 2px;color:var(--muted)}
button{box-shadow:inset 0 1px 0 var(--rim);border-radius:99px}button:hover{border-color:var(--crimson)}a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible{outline-color:var(--crimson)}
.missing-hero{padding:26px 24px;color:var(--ink);background:var(--paper)}.missing-hero .eyebrow{color:var(--muted)}.missing-hero h2{font-size:24px;margin:13px 0 18px;line-height:1.1;letter-spacing:-1px}.missing-hero strong{font-size:76px;letter-spacing:-4px;font-weight:650;color:var(--crimson)}.missing-hero p{font-size:12px;color:var(--ink)}.hero-note{color:var(--muted)}
.cal-board.card{padding:24px 0 0}.cal-month-head{padding:0 22px;align-items:center}.cal-month-head h2{font-size:15px;letter-spacing:-.3px;font-weight:500;display:flex;gap:12px;align-items:baseline;margin-top:8px}.cal-month-head h2 strong{font-size:60px;line-height:1.12;letter-spacing:-3px;font-weight:650;color:var(--crimson)}.cal-month-head .eyebrow{font-size:11px;margin:0}.cal-today-label{border:1px solid var(--line);color:var(--crimson);background:var(--rose);font-size:11px;padding:5px 9px;border-radius:99px}
.cal-legend{padding:17px 22px;gap:14px;font-size:11px}.cal-legend i{width:6px;height:6px;border-radius:50%}.cal-weekdays{padding:9px 0;background:transparent;font-size:10px;border-top-color:var(--line)}.cal-day{min-height:75px;padding:4px 2px;border-color:var(--line)}.cal-blank{background:transparent}.cal-date{font-size:11px;font-weight:600;width:24px;height:24px;margin:0 auto 4px;font-variant-numeric:tabular-nums}.cal-today{background:var(--rose)}.cal-today .cal-date{border:0;background:var(--crimson);color:var(--glass-solid);box-shadow:0 0 0 2px var(--paper);font-weight:750}.cal-chip{border-radius:5px;font-size:10px;line-height:1.2;min-height:40px}.cal-chip-title a,.cal-chip-title>span{min-width:0;min-height:40px;line-height:28px;padding:6px 2px 6px 12px;font-weight:600}.cal-check{left:2px;width:7px;height:7px;top:17px;font-size:7px}.cal-chips{gap:3px}.cal-todo{color:var(--ink);background:var(--sky)}.cal-grid-note{font-size:10px;padding:0 18px 14px;margin:12px 0 0}.cal-agenda-date{margin-bottom:10px;padding:0 3px}.cal-agenda-date h3{font-size:13px}.cal-event-row.card{border:0;box-shadow:none;border-radius:0;background:transparent;padding:18px 20px}.cal-event-row:before{display:none}.cal-agenda-day .task-list{display:block}
.mat-intro.card{padding:26px 24px;background:var(--paper);margin-bottom:24px}.mat-lead{font-size:21px;line-height:1.4;font-weight:650;margin:12px 0 20px;letter-spacing:-.65px}.mat-overall{font-size:14px;margin:0 0 20px;color:var(--crimson);line-height:1.4}.mat-overall strong{font-size:56px;font-weight:650;letter-spacing:-2.5px;margin-left:7px}.mat-overall strong span{font-size:27px;font-weight:450;letter-spacing:-1px}.mat-intro .muted{font-size:12px;line-height:1.85}.materials-list{gap:18px}.mat-course.card{padding:0}.mat-course>summary{padding:18px 20px;min-height:72px;gap:8px}.mat-course-name{font-size:16px;font-weight:700;letter-spacing:-.4px}.mat-counter{font-size:11px;font-weight:500;color:var(--muted)}.mat-weeks{padding:0 18px 10px}.mat-week>summary{min-height:62px;gap:7px}.mat-week-heading{font-size:13px;font-weight:650;gap:5px}.mat-latest{font-size:9px;color:var(--crimson);background:var(--rose);border-radius:4px;padding:3px 5px}.mat-row{min-height:60px;gap:8px}.mat-title{font-size:13px;line-height:1.6;letter-spacing:-.1px}.mat-check{min-height:48px;min-width:40px}.mat-check input{width:18px;height:18px}.mat-kind,.mat-kind svg{width:17px}.mat-kind{flex-basis:17px}.mat-subhead{font-size:11px;letter-spacing:.2px}.mat-hidden{font-size:11px;line-height:1.7}
.week-hero.card{padding:25px 23px}.week-progress-head h2 strong{font-size:66px;color:var(--ink);font-weight:650;letter-spacing:-3px}.week-progress-head h2>span{font-size:27px}.week-percent{font-size:29px;font-weight:600;letter-spacing:-1.5px;padding:7px 8px;border-radius:12px;background:var(--rose);color:var(--crimson)}.week-bar{height:10px;border:0;border-radius:99px;margin-top:25px;background:var(--line)}.week-bar>span{border-radius:99px}.week-encouragement{margin:16px 0 26px;font-size:12px}.week-days{padding-top:22px;gap:5px}.week-day-count{font-size:10px}.week-key{font-size:10px}.week-dot{width:32px;height:32px}.week-completed.card{padding:0}.week-completed-content .card{background:var(--paper)}
.score-overview{margin:0 0 24px;display:flex;align-items:center;justify-content:space-between;gap:15px;flex-wrap:wrap}.score-overview .eyebrow{margin:0 0 12px}.sq-average{font-size:58px;line-height:1.15;font-weight:650;letter-spacing:-2.5px;color:var(--crimson);font-variant-numeric:tabular-nums}.sq-average>span:not(.sq-number){font-size:24px;letter-spacing:-.5px;margin-left:3px}.score-overview p.muted{font-size:11px;margin:12px 0 0}.sq-toolbar{gap:12px;align-items:center}.sq-toolbar p{font-size:12px}.sq-toolbar button{border-radius:99px;font-size:11px;padding:10px 13px;min-height:44px}.sq-score-grid{gap:12px}.sq-score-card.card{padding:19px 16px;border-radius:20px}.sq-course-title{font-size:13px;min-height:44px;margin-bottom:16px;font-weight:650}.sq-score{font-size:31px;font-weight:650;letter-spacing:-1.7px}.sq-grade{color:var(--crimson);font-size:11px}.sq-score-bar{height:3px;margin-top:15px}.sq-score-bar>span{background:var(--crimson)}.sq-sparkline polyline{stroke:var(--crimson)}.sq-sparkline circle{fill:var(--crimson)}.sq-chart-caption{font-size:10px}.sq-results{padding:2px 20px;list-style:none}.sq-result-row{gap:10px}.sq-result-score{font-size:12px}.sq-result-percent{font-size:11px}.numbers-hidden .fx-metal{visibility:hidden!important}
.news-intro.card{background:var(--paper);padding:25px 23px}.news-stats{gap:6px 12px;margin:14px 0 16px;font-size:13px}.news-stats strong{font-size:37px;font-weight:650;letter-spacing:-2px;color:var(--crimson)}.news-intro .muted{font-size:11px}.news-entries.card{padding:0 22px}.news-course-head h2{font-size:14px}.news-entry-head h3{font-size:14px;line-height:1.65;font-weight:650}.news-excerpt{font-size:12px;color:var(--muted);line-height:1.85}.news-time{font-size:10px}.news-dot{background:var(--crimson)}
.sq-hero.card{background:var(--paper);border-color:var(--line);padding:24px 20px;grid-template-columns:122px minmax(0,1fr);gap:15px}.sq-character-stats h2{font-size:17px;font-weight:650;line-height:1.5}.sq-character-stats .eyebrow{color:var(--muted)}.sq-level{color:var(--crimson);background:var(--rose);font-size:12px;font-weight:700;letter-spacing:.8px;border-color:transparent}.sq-stat-label{font-size:11px}.sq-next-level{font-size:10px;line-height:1.7}.sq-quest-list{padding:0 18px}.sq-xp-reward{font-size:10px;padding:5px 7px}.sq-quest-row{gap:9px}.sq-game-rules{font-size:11px}.sq-quest-help,.sq-quest-intro{font-size:12px}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){.card,.task-list,.missing-hero,.empty,.sq-results,.sq-quest-list{background:var(--glass-solid)}.card:before,.task-list:before,.missing-hero:before,.sq-results:before,.sq-quest-list:before{backdrop-filter:none;background:linear-gradient(135deg,var(--rim),transparent 40%)}}
@media(min-width:760px){main{padding:48px 32px 40px}h1{font-size:36px;letter-spacing:-1.7px}.brandline{margin-bottom:30px}.page-head{margin-bottom:34px}.card{padding:30px}.task-row{padding:20px 24px}.mat-intro.card{padding:32px}.mat-overall strong{font-size:70px}.missing-hero{padding:32px}.sq-hero.card{padding:30px;grid-template-columns:180px minmax(0,1fr);gap:30px}.sq-score-card.card{padding:24px 20px}.sq-course-title{font-size:15px}.sq-score{font-size:38px}.sq-results{padding:4px 24px}.cal-day{min-height:83px;padding:6px 4px}.cal-date{margin:0 0 5px auto}.cal-chip-title a,.cal-chip-title>span{padding-left:15px}.cal-check{left:4px}.cal-chip{font-size:11px}.week-hero.card{padding:32px}.week-percent{font-size:36px}}
@media(min-width:1060px){.view-calendar main{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(0,1fr);gap:0 28px;align-items:start}.view-calendar .page-head,.view-calendar .page-foot{grid-column:1/-1}.view-calendar .cal-board{grid-column:1;grid-row:2}.view-calendar main>.section{grid-column:2;margin:0 0 26px}.view-calendar .cal-agenda-day{margin-bottom:20px}.view-calendar .cal-agenda-day .task-row,.view-calendar .cal-event-row.card{padding:16px 18px}.view-calendar .cal-month-head h2 strong{font-size:70px}.view-materials main{max-width:1100px;display:grid;grid-template-columns:minmax(0,.82fr) minmax(0,1.18fr);gap:0 28px;align-items:start}.view-materials .page-head,.view-materials .page-foot{grid-column:1/-1}.view-materials .mat-intro{position:sticky;top:28px}.view-materials .materials-list{grid-column:2}.view-materials .mat-overall strong{font-size:72px}}
@media(max-width:370px){main{padding-left:16px;padding-right:16px}.cal-month-head h2 strong{font-size:52px}.week-progress-head h2 strong{font-size:52px}.week-progress-head h2>span{font-size:23px}.week-percent{font-size:24px}.mat-course>summary{padding-left:16px;padding-right:16px}.sq-hero.card{grid-template-columns:104px minmax(0,1fr);padding:22px 16px;gap:10px}}
'''


# Original, self-contained shaders; no third-party shader source copied.
_FX_CREDIT = '''<!-- Visual technique references: ruucm/shadergradient (package MIT, no root LICENSE: idea only); paper-design/liquid-logo (PolyForm Shield 1.0.0: idea only); pmndrs/react-three-fiber (MIT, Poimandres: scene and render-loop concepts only). All GLSL and WebGL code below is original. No React, Three.js, fonts, or network assets are included. -->'''

_FX_HTML = '<canvas class="fx-bg" aria-hidden="true" data-fx="pending"></canvas>'

_FX_CSS = r'''
body{isolation:isolate;background:radial-gradient(ellipse at 92% 4%,#e6b9ae 0,transparent 45%),radial-gradient(ellipse at 8% 75%,#eed5c2 0,transparent 52%),#faf1e8}
body>main{position:relative;z-index:1}.fx-bg{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;opacity:.56}
.fx-metal-target{position:relative;display:inline-block;vertical-align:baseline}.fx-metal{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:1}.numbers-hidden .fx-metal{visibility:hidden!important}
@media(prefers-color-scheme:dark){body{background:radial-gradient(ellipse at 95% 0,#4b102b 0,transparent 50%),radial-gradient(ellipse at 5% 80%,#351423 0,transparent 55%),#160c14}.fx-bg{opacity:.7}}
@media print{.fx-bg,.fx-metal{display:none!important}}
'''

_FX_JS = r'''
(function(){
'use strict';
var bg=document.querySelector('.fx-bg');if(!bg)return;
var dark=matchMedia('(prefers-color-scheme: dark)'),reduced=matchMedia('(prefers-reduced-motion: reduce)');
var state=window.__oscFx={background:false,metal:false,frames:0,metalFrames:0,fpsLimit:24,dpr:1,reduced:reduced.matches,paused:document.hidden,errors:[]};
var jobs=[],raf=0,last=-1000,elapsed=0,previous=0;
function program(canvas,vertex,fragment){
 var gl=canvas.getContext('webgl',{alpha:true,antialias:false,preserveDrawingBuffer:true,powerPreference:'low-power'});
 if(!gl){canvas.dataset.fx='fallback';canvas.style.display='none';return null;}
 function shader(type,src){var s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){throw new Error(gl.getShaderInfoLog(s));}return s;}
 var p=gl.createProgram();gl.attachShader(p,shader(gl.VERTEX_SHADER,vertex));gl.attachShader(p,shader(gl.FRAGMENT_SHADER,fragment));gl.linkProgram(p);
 if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(p));gl.useProgram(p);
 canvas.dataset.fx='ready';canvas.addEventListener('webglcontextlost',function(e){e.preventDefault();canvas.style.display='none';canvas.dataset.fx='fallback';});
 return {gl:gl,p:p,uniform:function(n){return gl.getUniformLocation(p,n);}};
}
function buffer(gl,p,data){var b=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(data),gl.STATIC_DRAW);var a=gl.getAttribLocation(p,'a');gl.enableVertexAttribArray(a);gl.vertexAttribPointer(a,2,gl.FLOAT,false,0,0);}
var noise='float hash3(vec3 p){return fract(sin(dot(p,vec3(17.17,71.31,153.17)))*17351.719);}float noise3(vec3 p){vec3 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);float a=mix(hash3(i),hash3(i+vec3(1,0,0)),f.x);float b=mix(hash3(i+vec3(0,1,0)),hash3(i+vec3(1,1,0)),f.x);float c=mix(hash3(i+vec3(0,0,1)),hash3(i+vec3(1,0,1)),f.x);float d=mix(hash3(i+vec3(0,1,1)),hash3(i+vec3(1,1,1)),f.x);return mix(mix(a,b,f.y),mix(c,d,f.y),f.z);}';
try{
 var scene=program(bg,'precision mediump float;attribute vec2 a;uniform float t,aspect;varying vec2 uv;varying float elevation;'+noise+'void main(){uv=a*.5+.5;vec3 q=vec3(a*1.14,t*.035);float n=noise3(q)+.34*noise3(q*2.7+4.0);float z=(n-.65)*1.8;vec3 world=vec3(a.x*aspect*5.6,a.y*5.6,z);float cy=cos(.18),sy=sin(.18);world.yz=mat2(cy,-sy,sy,cy)*world.yz;float depth=6.0-world.z;gl_Position=vec4(world.x*1.6/aspect,world.y*1.6,0.0,depth);elevation=n;}',
 'precision mediump float;uniform float t,darkMode;varying vec2 uv;varying float elevation;'+noise+'void main(){float drift=noise3(vec3(uv*2.0,t*.022));float bloom=smoothstep(.42,.92,drift+.16*elevation);vec3 cream=vec3(.945,.839,.761),beige=vec3(.867,.655,.545),crimson=vec3(.545,0.,.157),ink=vec3(.086,.016,.024);vec3 light=mix(mix(cream,vec3(1.0),.53),beige,.19*bloom);light=mix(light,crimson,.15*bloom*bloom);vec3 night=mix(ink,crimson,.09+.29*bloom);night+=vec3(.018,.008,.023)*elevation;gl_FragColor=vec4(mix(light,night,darkMode),1.0);}');
 if(scene){var mesh=[],nx=56,ny=48;for(var y=0;y<ny;y++)for(var x=0;x<nx;x++){var x0=x/nx*2-1,x1=(x+1)/nx*2-1,y0=y/ny*2-1,y1=(y+1)/ny*2-1;mesh.push(x0,y0,x1,y0,x0,y1,x0,y1,x1,y0,x1,y1);}buffer(scene.gl,scene.p,mesh);var st=scene.uniform('t'),sa=scene.uniform('aspect'),sd=scene.uniform('darkMode');
 jobs.push(function(time){var gl=scene.gl,w=Math.max(1,innerWidth),h=Math.max(1,innerHeight);if(bg.width!==w||bg.height!==h){bg.width=w;bg.height=h;}gl.viewport(0,0,w,h);gl.uniform1f(st,time);gl.uniform1f(sa,w/h);gl.uniform1f(sd,dark.matches?1:0);gl.drawArrays(gl.TRIANGLES,0,mesh.length/2);state.frames++;});state.background=true;}
}catch(e){state.errors.push(String(e));bg.dataset.fx='fallback';bg.style.display='none';}
var target=document.querySelector('.missing-hero strong,.cal-month-head h2 strong,#mat-overall,.week-percent,.sq-average,.news-stats strong,.sq-level');
if(!target&&document.body.classList.contains('view-scores'))target=document.querySelector('.sq-score');
if(target){try{
 var wrap=document.createElement('span');wrap.className='fx-metal-target';target.parentNode.insertBefore(wrap,target);wrap.appendChild(target);
 var metal=document.createElement('canvas');metal.className='fx-metal';metal.setAttribute('aria-hidden','true');metal.dataset.fx='pending';wrap.appendChild(metal);
 var m=program(metal,'attribute vec2 a;varying vec2 uv;void main(){uv=a*.5+.5;gl_Position=vec4(a,0,1);}',
 'precision mediump float;varying vec2 uv;uniform sampler2D mask;uniform vec2 pixel;uniform float t,darkMode;void main(){vec2 p=vec2(uv.x,1.-uv.y);float alpha=texture2D(mask,p).a;if(alpha<.01){gl_FragColor=vec4(0);return;}float gx=texture2D(mask,p+vec2(pixel.x,0)).a-texture2D(mask,p-vec2(pixel.x,0)).a;float gy=texture2D(mask,p+vec2(0,pixel.y)).a-texture2D(mask,p-vec2(0,pixel.y)).a;float wave=sin(p.x*10.5+p.y*5.2+sin(p.y*7.8-t*.37)*1.45+t*.21);float sheen=smoothstep(.22,.72,wave);float rim=clamp(abs(gx)+abs(gy),0.,1.);vec3 low=mix(vec3(.13,.025,.065),vec3(.77,.67,.63),darkMode);vec3 high=mix(vec3(.34,.12,.18),vec3(1.,.94,.86),darkMode);vec3 color=mix(low,high,clamp(.14+sheen*.69+rim*.17,0.,1.));gl_FragColor=vec4(color,alpha);}');
 if(m){buffer(m.gl,m.p,[-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]);var tex=m.gl.createTexture();m.gl.bindTexture(m.gl.TEXTURE_2D,tex);m.gl.texParameteri(m.gl.TEXTURE_2D,m.gl.TEXTURE_WRAP_S,m.gl.CLAMP_TO_EDGE);m.gl.texParameteri(m.gl.TEXTURE_2D,m.gl.TEXTURE_WRAP_T,m.gl.CLAMP_TO_EDGE);m.gl.texParameteri(m.gl.TEXTURE_2D,m.gl.TEXTURE_MIN_FILTER,m.gl.LINEAR);m.gl.texParameteri(m.gl.TEXTURE_2D,m.gl.TEXTURE_MAG_FILTER,m.gl.LINEAR);
 var mask=document.createElement('canvas'),ctx=mask.getContext('2d'),mt=m.uniform('t'),md=m.uniform('darkMode'),mp=m.uniform('pixel'),dirty=true,lastText='';
 function updateMask(){
  var r=wrap.getBoundingClientRect(),w=Math.max(1,Math.ceil(r.width)),h=Math.max(1,Math.ceil(r.height)),text=target.textContent;
  if(!dirty&&text===lastText&&metal.width===w&&metal.height===h)return;
  dirty=false;lastText=text;metal.width=mask.width=w;metal.height=mask.height=h;ctx.textAlign='left';ctx.textBaseline='alphabetic';ctx.fillStyle='#fff';
  var walker=document.createTreeWalker(target,NodeFilter.SHOW_TEXT),node,range=document.createRange();
  while((node=walker.nextNode())){
   var s=getComputedStyle(node.parentElement);if(s.visibility==='hidden'||s.display==='none')continue;
   ctx.font=s.fontStyle+' '+s.fontWeight+' '+s.fontSize+' '+s.fontFamily;
   if('fontKerning' in ctx)ctx.fontKerning=s.fontKerning;
   if('letterSpacing' in ctx)ctx.letterSpacing=s.letterSpacing==='normal'?'0px':s.letterSpacing;
   var offset=0;
   Array.from(node.nodeValue).forEach(function(letter){
    range.setStart(node,offset);offset+=letter.length;range.setEnd(node,offset);
    if(!letter.trim())return;var rect=range.getBoundingClientRect();if(!rect.width||!rect.height)return;
    var metric=ctx.measureText(letter),ascent=metric.fontBoundingBoxAscent||parseFloat(s.fontSize)*.8,descent=metric.fontBoundingBoxDescent||parseFloat(s.fontSize)*.2;
    var baseline=rect.top-r.top+(rect.height-ascent-descent)/2+ascent;
    ctx.fillText(letter,rect.left-r.left,baseline);
   });
  }
  m.gl.bindTexture(m.gl.TEXTURE_2D,tex);m.gl.texImage2D(m.gl.TEXTURE_2D,0,m.gl.RGBA,m.gl.RGBA,m.gl.UNSIGNED_BYTE,mask);m.gl.uniform2f(mp,1/w,1/h);
 }
 jobs.push(function(time){updateMask();var gl=m.gl;gl.viewport(0,0,metal.width,metal.height);gl.uniform1f(mt,time);gl.uniform1f(md,dark.matches?1:0);gl.drawArrays(gl.TRIANGLES,0,6);state.metalFrames++;});state.metal=true;
 new MutationObserver(function(){dirty=true;invalidate();}).observe(target,{childList:true,characterData:true,subtree:true});
 if(window.ResizeObserver)new ResizeObserver(function(){dirty=true;invalidate();}).observe(wrap);
 }
}catch(e){state.errors.push(String(e));if(metal){metal.dataset.fx='fallback';metal.style.display='none';}}}
function draw(){jobs.forEach(function(job){job(elapsed);});}
function tick(ms){raf=0;if(document.hidden||reduced.matches)return;if(ms-last>=1000/24){if(previous)elapsed+=Math.min(.1,(ms-previous)/1000);previous=ms;last=ms;draw();}raf=requestAnimationFrame(tick);}
function invalidate(){if(document.hidden)return;draw();if(!reduced.matches&&!raf)raf=requestAnimationFrame(tick);}
function mode(){state.reduced=reduced.matches;cancelAnimationFrame(raf);raf=0;previous=0;invalidate();}
document.addEventListener('visibilitychange',function(){state.paused=document.hidden;if(document.hidden){cancelAnimationFrame(raf);raf=0;previous=0;}else invalidate();});
if(dark.addEventListener){dark.addEventListener('change',mode);reduced.addEventListener('change',mode);}else{dark.addListener(mode);reduced.addListener(mode);}
addEventListener('resize',invalidate);invalidate();
})();
'''


def _page(slug, data, now, body, css='', js='', wide=False):
    title = next((entry[1] for entry in UIS.values() if entry[0] == slug), '나의 LMS')
    generated = _dt(data.get('generated_at')) or _dt(now)
    badge = '<span class="sample-badge">가짜 데이터</span>' if data.get('source') == 'sample' else ''
    footer = f'고려대 LMS에서 {_date(generated)}에 받음 · 조회만 했어요 · OSC 고려대 Workshop 01'
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_e(title)}</title>{_GLASS_CREDIT}{_FX_CREDIT}'
            f'<style>{_VIEW_CSS}\n{css}\n{_POLISH_CSS}\n{_FX_CSS}</style></head>'
            f'<body class="view-{_e(slug)}">{_GLASS_SVG}{_FX_HTML}<main class="{"wide" if wide else "column"}">'
            f'<header class="page-head"><div class="brandline"><div class="wordmark">OSC · WORKSHOP 01</div>{badge}</div>'
            f'<h1>{_e(title)}</h1><p class="head-date">{_e(_date(now, False))} · 나의 캠퍼스 한눈에</p></header>'
            f'{body}<footer class="page-foot"><p>{_e(footer)}</p></footer></main>'
            + (f'<script>{js}</script>' if js else '') + f'<script>{_FX_JS}</script></body></html>')


def render_missing(data: dict, now: datetime) -> str:
    now = _dt(now)
    horizon = now + timedelta(days=14)
    all_tasks = _items(data, 'tasks')
    pending = [t for t in all_tasks if not _done(t)]
    visible = [t for t in pending if t.get('status') == 'missing' or _dt(t.get('due_at')) is None or _dt(t.get('due_at')) <= horizon]
    visible.sort(key=lambda t: _dt(t.get('due_at')) or datetime.max.replace(tzinfo=KST))
    nearest_task = next((t for t in visible if _dt(t.get('due_at')) and _dt(t.get('due_at')) >= now), None)
    nearest = ('가장 가까운 마감 · ' + _date(nearest_task.get('due_at'))) if nearest_task else '다가오는 마감은 아직 없어요'
    hero = (f'<div class="missing-hero"><div class="eyebrow">지금 챙길 것</div><h2>안 낸 것 <strong>{len(visible)}</strong>개</h2>'
            f'<p>{_e(nearest)}</p><span class="hero-note">지난 마감과 앞으로 14일을 모았어요</span></div>')
    groups = [[], [], [], [], []]
    week_end = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    for task in visible:
        due = _dt(task.get('due_at'))
        if task.get('status') == 'missing' or (due is not None and due < now):
            groups[0].append(task)
        elif due is None:
            groups[4].append(task)
        elif due <= now + timedelta(hours=48):
            groups[1].append(task)
        elif due < week_end:
            groups[2].append(task)
        else:
            groups[3].append(task)
    sections = []
    for label, rows in zip(('지난 마감 · 안 냄', '48시간 안', '이번 주', '그 뒤 (14일 안)', '마감 미정'), groups):
        if rows:
            sections.append(_section(f'{label} · {len(rows)}개', '<ul class="task-list">' + ''.join(_task_row(t, now) for t in rows) + '</ul>'))
    if not visible:
        sections.append(_empty('14일 안에 낼 게 없어요. 잠깐 숨을 돌려요.'))
    later = len(pending) - len(visible)
    if later:
        sections.append(f'<p class="muted horizon-note">14일 뒤에 마감인 {later}개는 이 목록에서 뺐어요.</p>')
    submitted = sorted((t for t in all_tasks if _done(t)), key=lambda t: _dt(t.get('due_at')) or datetime.min.replace(tzinfo=KST), reverse=True)
    completed = ('<ul class="task-list">' + ''.join(_task_row(t, now) for t in submitted) + '</ul>') if submitted else _empty('아직 낸 것으로 표시된 항목이 없어요.')
    body = hero + ''.join(sections) + ('<div class="done-toggle"><input type="checkbox" id="show-done">'
            f'<label for="show-done">낸 것도 보기 <span class="muted">{len(submitted)}개</span></label><div class="done-content">{completed}</div></div>')
    css = '''.missing-hero{padding:23px;border-radius:20px;background:var(--rose);color:var(--crimson)}.missing-hero .eyebrow{color:var(--crimson)}.missing-hero h2{font-size:24px;font-weight:600;letter-spacing:-1px;margin:9px 0 12px;line-height:1.1}.missing-hero strong{font-size:55px;font-weight:800;letter-spacing:-3px}.missing-hero p{font-size:12px;font-weight:700;margin:0 0 6px}.hero-note{font-size:11px;opacity:.85}
.done-toggle{display:grid;grid-template-columns:24px 1fr;gap:0 7px;align-items:center;margin-top:24px}.done-toggle>input{width:20px;height:20px;margin:0;position:relative;z-index:1}.done-toggle>label{display:flex;align-items:center;gap:9px;min-height:44px;cursor:pointer;padding-left:31px;margin-left:-31px;font-weight:700;font-size:13px}.done-content{display:none;grid-column:1/-1;margin-top:8px}#show-done:checked~.done-content{display:block}.horizon-note{font-size:11px}'''
    return _page('missing', data, now, body, css)


def _md(value):
    text = ' '.join(str(value if value is not None else '').split())
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', _e(text))


def _md_title(item):
    title = _md(item.get('title') or '제목 없는 항목')
    url = str(item.get('url') or '').strip()
    try:
        safe = urllib.parse.urlsplit(url).scheme.lower() in ('http', 'https')
    except ValueError:
        safe = False
    if safe:
        return f'[{title}]({_e(urllib.parse.quote(url, safe=":/?&=%#@+;,~"))})'
    return title


def render_briefing(data: dict, now: datetime) -> str:
    now = _dt(now)
    tasks = _items(data, 'tasks')
    upcoming = sorted((t for t in tasks if not _done(t) and _dt(t.get('due_at')) is not None
                       and now <= _dt(t.get('due_at')) <= now + timedelta(days=7)), key=lambda t: _dt(t.get('due_at')))
    missing = [t for t in tasks if t.get('status') == 'missing']
    news = sorted((a for a in _items(data, 'announcements') if _dt(a.get('posted_at')) is not None
                   and now - timedelta(hours=24) <= _dt(a.get('posted_at')) <= now), key=lambda a: _dt(a.get('posted_at')), reverse=True)
    lines = [f'**{UIS[3][1]}**' + (' · **가짜 데이터**' if data.get('source') == 'sample' else ''),
             f'{_date(now, False)}, 좋은 하루예요.', f'**이번 주 낼 것 {len(upcoming)}개**']
    for t in upcoming[:4]:
        lines.append(f'- {_md(_due_label(t.get("due_at"), now))} · {_md(t.get("course") or "과목 정보 없음")} · {_md_title(t)}')
    if len(upcoming) > 4:
        lines.append(f'- 그 밖에 {len(upcoming) - 4}개가 더 있어요.')
    if not upcoming:
        lines.append('- 7일 안에 낼 게 없어요.')
    if missing:
        first = missing[0]
        rest = f' 외 {len(missing) - 1}개' if len(missing) > 1 else ''
        lines.append(f'**놓친 것 {len(missing)}개** · {_md(first.get("course") or "과목 정보 없음")} · {_md_title(first)}{rest}')
    lines.append(f'**새 공지 {len(news)}개** · 최근 24시간이에요')
    for a in news[:3]:
        lines.append(f'- {_md(a.get("course") or "과목 정보 없음")} · {_md_title(a)}')
    unread = _number(data.get('unread_messages'))
    if unread is not None:
        lines.append(f'안 읽은 메시지 {max(0, int(unread))}개 · LMS 받은 편지함에서 확인해요.')
    lines.append(f'고려대 LMS에서 {_date(_dt(data.get("generated_at")) or now)}에 받음 · 조회만 했어요 · OSC 고려대 Workshop 01')
    return '\n'.join(lines)

def _materials_icon(kind):
    """Small original line icons; labels remain available to screen readers."""
    paths = {
        "file": '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>',
        "page": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
        "link": '<path d="M10 14l4-4M8 16l-1 1a4 4 0 01-6-6l4-4a4 4 0 016 0M16 8l1-1a4 4 0 016 6l-4 4a4 4 0 01-6 0"/>',
        "quiz": '<circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 016 1c0 2-3 2-3 4M12 17h.01"/>',
        "assignment": '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M8 12l3 3 5-6"/>',
        "discussion": '<path d="M4 4h16v12H9l-5 4zM8 8h8M8 12h5"/>',
    }
    labels = {"file": "파일", "page": "페이지", "link": "링크", "quiz": "퀴즈",
              "assignment": "과제", "discussion": "토론"}
    icon = paths.get(kind, paths["page"])
    return ('<span class="mat-kind" title="' + labels.get(kind, "자료") + '">'
            '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
            'stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">' + icon + '</svg></span>')


def render_materials(data: dict, now: datetime) -> str:
    modules = _items(data, "modules")
    groups = {}
    for module in modules:
        key = str(module.get("course_id")) if module.get("course_id") is not None else str(module.get("course") or "unknown")
        groups.setdefault(key, []).append(module)
    blocks = []
    grand_total = grand_done = 0
    for course_index, course_modules in enumerate(groups.values()):
        course_modules = sorted(course_modules, key=lambda m: _number(m.get("position"), -1), reverse=True)
        # 아직 자료가 없는 주차(앞으로 올 주차 등)는 숨기고, 자료가 있는 가장 최근 주차를 펼쳐요.
        filled = [m for m in course_modules if any(i.get("kind") != "header" for i in _items(m, "items"))]
        hidden_empty = len(course_modules) - len(filled)
        name_module = course_modules[0]
        course_modules = filled
        course_total = course_done = 0
        weeks = []
        for week_index, module in enumerate(course_modules):
            items = _items(module, "items")
            counted = [item for item in items if item.get("kind") != "header"]
            week_total = len(counted)
            week_done = sum(item.get("done") is True for item in counted)
            course_total += week_total
            course_done += week_done
            rows = []
            for item_index, item in enumerate(items):
                title = item.get("title") or "이름 없는 자료"
                if item.get("kind") == "header":
                    rows.append('<li class="mat-subhead">' + _link(item, title) + '</li>')
                    continue
                checkbox_id = f"material-{course_index}-{week_index}-{item_index}"
                saved_key = ("osc-material:" + str(item.get("id"))) if item.get("id") is not None else ""
                checked = " checked" if item.get("done") is True else ""
                rows.append('<li class="mat-row"><label class="mat-check" for="' + checkbox_id + '">'
                            '<input type="checkbox" id="' + checkbox_id + '" data-material-key="' + _e(saved_key)
                            + '" aria-label="' + _e(str(title) + " 읽음 표시") + '"' + checked + '></label>'
                            + _materials_icon(item.get("kind")) + '<div class="mat-title">'
                            + _link(item, title) + '</div></li>')
            week_name = module.get("name") or "주차 이름 없음"
            opened = " open" if week_index == 0 else ""
            latest = '<span class="mat-latest">최근 주차</span>' if week_index == 0 else ""
            weeks.append('<details class="mat-week"' + opened + '><summary><span class="mat-week-heading">'
                         '<span>' + _e(week_name) + '</span>' + latest + '</span><span class="mat-counter" '
                         'aria-live="polite">본 것 ' + str(week_done) + '/' + str(week_total) + '</span></summary>'
                         + ('<ul class="mat-items">' + ''.join(rows) + '</ul>' if rows else _empty("아직 등록된 자료가 없어요"))
                         + '</details>')
        grand_total += course_total
        grand_done += course_done
        blocks.append('<details class="card mat-course" open><summary><span class="mat-course-name">'
                      + _course(name_module) + '</span><span class="mat-counter" aria-live="polite">본 것 '
                      + str(course_done) + '/' + str(course_total) + '</span></summary><div class="mat-weeks">'
                      + ''.join(weeks)
                      + (f'<p class="muted mat-hidden">자료가 아직 없는 주차 {hidden_empty}개는 숨겼어요.</p>' if hidden_empty else '')
                      + '</div></details>')
    intro = ('<section class="card mat-intro"><div class="eyebrow">내 학습 체크리스트</div>'
             '<p class="mat-lead">한 장씩, 차근차근 봐요</p><p class="mat-overall">본 것 <strong id="mat-overall">'
             + str(grand_done) + '<span> / ' + str(grand_total) + '</span></strong></p>'
             '<p class="muted">최근 주차부터 펼쳐 두었어요.<br>체크는 이 브라우저에만 기억해요.</p>'
             '<noscript><p class="muted">체크는 할 수 있어요. 저장과 개수 갱신은 JavaScript를 켜면 돼요.</p></noscript></section>')
    body = intro + '<div class="materials-list">' + (''.join(blocks) if blocks else _empty("아직 볼 자료가 없어요. 새 자료가 올라오면 여기서 확인해요.")) + '</div>'
    css = '''
.mat-intro{background:var(--mint);margin-bottom:24px}.mat-lead{font-size:21px;font-weight:750;margin:8px 0 14px;letter-spacing:-.5px}.mat-overall{font-size:14px;margin:0 0 14px;color:var(--green)}.mat-overall strong{font-size:34px;font-weight:800;margin-left:7px;letter-spacing:-1.2px}.mat-overall strong span{font-size:23px;font-weight:500}.mat-intro .muted{font-size:13px;line-height:1.7;margin:0}.materials-list{display:grid;gap:16px}.mat-course{padding:0;overflow:hidden}.mat-course>summary{padding:18px 20px;display:flex;align-items:center;gap:10px;cursor:pointer;min-height:62px;list-style:none}.mat-course>summary::-webkit-details-marker,.mat-week>summary::-webkit-details-marker{display:none}.mat-course-name{font-size:16px;font-weight:750;flex:1;min-width:0;overflow-wrap:anywhere}.mat-course>summary:after,.mat-week>summary:after{content:'⌄';font-size:18px;color:var(--muted);margin-left:3px;transform:rotate(-90deg)}.mat-course[open]>summary:after,.mat-week[open]>summary:after{transform:rotate(0)}.mat-counter{font-size:12px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums;flex-shrink:0}.mat-weeks{border-top:1px solid var(--line);padding:0 16px 8px}.mat-week+ .mat-week{border-top:1px solid var(--line)}.mat-week>summary{display:flex;align-items:center;gap:8px;list-style:none;min-height:54px;cursor:pointer;padding:9px 0}.mat-week-heading{display:flex;gap:8px;align-items:center;flex-wrap:wrap;min-width:0;flex:1;font-size:14px;font-weight:650;overflow-wrap:anywhere}.mat-latest{font-size:10px;white-space:nowrap;background:var(--sky);color:var(--blue);padding:3px 6px;border-radius:4px;font-weight:650}.mat-items{list-style:none;margin:0;padding:0 0 10px}.mat-row{display:flex;align-items:center;gap:9px;min-height:52px;border-top:1px solid var(--line)}.mat-check{display:flex;align-items:center;justify-content:center;min-width:40px;min-height:44px;cursor:pointer;margin-left:-8px}.mat-check input{width:18px;height:18px;accent-color:var(--green);cursor:pointer}.mat-kind{display:flex;color:var(--muted);width:19px;flex:0 0 19px}.mat-kind svg{width:19px;height:19px}.mat-title{font-size:13px;line-height:1.5;min-width:0;flex:1;overflow-wrap:anywhere}.mat-title a{display:flex;align-items:center;min-height:44px;padding:5px 0;color:var(--ink)}.mat-row:has(input:checked) .mat-title{color:var(--muted)}.mat-row:has(input:checked) .mat-title a{color:var(--muted)}.mat-subhead{font-size:12px;font-weight:700;color:var(--muted);padding:13px 0 9px;overflow-wrap:anywhere}.mat-subhead a{display:flex;align-items:center;min-height:40px;color:inherit}.mat-week .empty{padding:15px 0;font-size:13px}.mat-hidden{font-size:12px;margin:10px 0 6px}.mat-course summary:focus-visible{outline:2px solid var(--blue);outline-offset:-3px}
'''
    js = '''
(function(){
 const inputs=Array.from(document.querySelectorAll('input[data-material-key]'));
 function update(){
   document.querySelectorAll('.mat-week,.mat-course').forEach(function(group){
     const checks=Array.from(group.querySelectorAll('input[data-material-key]'));
     const counter=group.querySelector(':scope > summary > .mat-counter');
     if(counter)counter.textContent='본 것 '+checks.filter(c=>c.checked).length+'/'+checks.length;
   });
   const overall=document.getElementById('mat-overall');
   if(overall){overall.textContent=inputs.filter(c=>c.checked).length;const total=document.createElement('span');total.textContent=' / '+inputs.length;overall.appendChild(total);}
 }
 inputs.forEach(function(input){
   if(input.dataset.materialKey){try{const saved=localStorage.getItem(input.dataset.materialKey);if(saved==='1'||saved==='0')input.checked=saved==='1';}catch(e){}}
   input.addEventListener('change',function(){
     if(input.dataset.materialKey){try{localStorage.setItem(input.dataset.materialKey,input.checked?'1':'0');}catch(e){}}
     update();
   });
 });
 update();
})();
'''
    return _page("materials", data, now, body, css, js)


def _news_age(value, now):
    posted = _dt(value)
    if posted is None:
        return "게시 시각 없음"
    elapsed = (now - posted).total_seconds()
    if elapsed < 0:
        return _date(value)
    if elapsed < 60:
        return "방금 전"
    if elapsed < 3600:
        return f"{int(elapsed // 60)}분 전"
    if elapsed < 86400:
        return f"{int(elapsed // 3600)}시간 전"
    return f"{int(elapsed // 86400)}일 전"


def render_news(data: dict, now: datetime) -> str:
    announcements = _items(data, "announcements")
    def timestamp(item):
        posted = _dt(item.get("posted_at"))
        return posted.timestamp() if posted else float("-inf")
    announcements = sorted(announcements, key=timestamp, reverse=True)
    fresh = sum(1 for item in announcements if _dt(item.get("posted_at")) is not None
                and 0 <= (now - _dt(item.get("posted_at"))).total_seconds() <= 86400)
    unread = data.get("unread_messages")
    messages = ('<span class="news-stat-divider">·</span><span>안 읽은 메시지 <strong>' + _e(unread)
                + '</strong>개</span>') if unread is not None else ''
    intro = ('<section class="card news-intro"><div class="eyebrow">최근 24시간</div><p class="news-stats">'
             '<span>새 공지 <strong>' + str(fresh) + '</strong>개</span>' + messages
             + '</p><p class="muted">메시지는 LMS 받은 편지함에서 확인해요.</p></section>')
    groups = {}
    for item in announcements:
        key = str(item.get("course_id")) if item.get("course_id") is not None else str(item.get("course") or "unknown")
        groups.setdefault(key, []).append(item)
    sections = []
    for rows in groups.values():
        entries = []
        for item in rows:
            unread_dot = ('<span class="news-dot" role="img" aria-label="안 읽은 공지"></span>'
                          if item.get("unread") is True else '<span class="news-dot news-read" aria-hidden="true"></span>')
            date = _dt(item.get("posted_at"))
            date_attr = ' datetime="' + _e(date.isoformat()) + '"' if date else ''
            entries.append('<article class="news-entry"><div class="news-entry-head">' + unread_dot
                           + '<h3>' + _link(item, item.get("title") or "제목 없는 공지") + '</h3></div>'
                           '<time class="news-time"' + date_attr + '>' + _e(_news_age(item.get("posted_at"), now))
                           + '</time><p class="news-excerpt">' + _e(item.get("text") or "본문은 LMS에서 확인해요.")
                           + '</p></article>')
        sections.append('<section class="news-course"><div class="news-course-head"><h2>' + _course(rows[0])
                        + '</h2><span class="pill">' + str(len(rows)) + '개</span></div><div class="card news-entries">'
                        + ''.join(entries) + '</div></section>')
    body = intro + (''.join(sections) if sections else _empty("새로 올라온 공지가 없어요. 잠깐 숨을 돌려도 좋아요."))
    css = '''
.news-intro{background:var(--sky);margin-bottom:28px}.news-stats{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;font-size:14px;line-height:1.7;margin:9px 0 10px}.news-stats strong{font-size:27px;color:var(--blue);font-weight:800;letter-spacing:-1px;margin:0 2px}.news-stat-divider{color:var(--muted)}.news-intro .muted{font-size:12px;margin:0;line-height:1.7}.news-course{margin:0 0 25px}.news-course-head{display:flex;align-items:center;gap:10px;margin-bottom:10px;padding:0 3px}.news-course-head h2{font-size:14px;font-weight:750;min-width:0;flex:1;overflow-wrap:anywhere;margin:0}.news-course-head .pill{font-size:11px;color:var(--muted)}.news-entries{padding:0 19px}.news-entry{padding:17px 0 19px;position:relative}.news-entry+.news-entry{border-top:1px solid var(--line)}.news-entry-head{display:flex;align-items:baseline;gap:9px}.news-dot{height:7px;width:7px;flex:0 0 7px;border-radius:50%;background:var(--blue);position:relative;top:-1px}.news-read{background:transparent}.news-entry h3{font-size:15px;line-height:1.5;font-weight:750;min-width:0;flex:1;margin:0;overflow-wrap:anywhere}.news-entry h3 a{display:flex;align-items:center;min-height:40px;color:var(--ink)}.news-time{display:block;margin:0 0 8px 16px;font-size:11px;color:var(--muted)}.news-excerpt{font-size:13px;line-height:1.7;color:var(--muted);margin:0 0 0 16px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden;overflow-wrap:anywhere;white-space:normal}.news-entry h3 a:hover{color:var(--blue)}
'''
    return _page("news", data, now, body, css)


def render_calendar(data: dict, now: datetime) -> str:
    """Sunday-first month overview, with a readable agenda at every width."""
    local_now = now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)
    first = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    after = (first + timedelta(days=32)).replace(day=1)
    days = (after - first).days
    grouped = {}
    undated = []
    for kind, key, date_key in (("task", "tasks", "due_at"), ("event", "events", "start_at")):
        for item in _items(data, key):
            stamp = _dt(item.get(date_key))
            if stamp is None:
                undated.append((kind, item))
            elif first <= stamp < after:
                grouped.setdefault(stamp.day, []).append((stamp, kind, item))
    for entries in grouped.values():
        entries.sort(key=lambda entry: (entry[0], entry[1]))

    def event_row(item):
        start = _dt(item.get("start_at"))
        end = _dt(item.get("end_at"))
        if item.get("all_day") is True:
            when = "하루 종일"
        elif start:
            when = start.strftime("%H:%M")
            if end and end >= start:
                when += "–" + (end.strftime("%H:%M") if end.date() == start.date() else _date(item.get("end_at")))
        else:
            when = "날짜가 아직 없어요"
        return ('<li class="card cal-event-row"><span class="cal-event-symbol" aria-hidden="true">◇</span>'
                '<div class="cal-event-copy"><div class="course">' + _course(item) + '</div>'
                '<div class="task-title">' + _link(item) + '</div>'
                '<div class="meta"><span class="pill cal-event-label">일정</span><span>' + _e(when) + '</span></div></div></li>')

    cells = []
    leading = (first.weekday() + 1) % 7
    for unused in range(leading):
        cells.append('<div class="cal-day cal-blank" aria-hidden="true"></div>')
    for day in range(1, days + 1):
        date = first.replace(day=day)
        is_today = day == local_now.day
        date_class = " cal-today" if is_today else ""
        date_current = ' aria-current="date"' if is_today else ""
        chips = []
        for stamp, kind, item in grouped.get(day, []):
            if kind == "event":
                chip_class, mark, state = "cal-event", "◇", "일정"
            elif _done(item):
                chip_class, mark, state = "cal-done", "✓", "제출 완료 또는 면제"
            elif item.get("status") == "missing":
                chip_class, mark, state = "cal-missing", "", "안 냄"
            else:
                chip_class, mark, state = "cal-todo", "", "낼 것"
            title = item.get("title") or "제목 없는 항목"
            title_markup = _link(item, cls="cal-chip-link")
            chips.append('<div class="cal-chip ' + chip_class + '" title="' + _e(state + " · " + str(title)) + '">'
                         '<span class="cal-check" aria-hidden="true">' + mark + '</span>'
                         '<span class="cal-chip-title">' + title_markup + '</span></div>')
        cells.append('<div class="cal-day' + date_class + '"' + date_current + '>'
                     '<span class="cal-date">' + str(day) + '</span><div class="cal-chips">' + ''.join(chips) + '</div></div>')
    trailing = (-len(cells)) % 7
    cells.extend('<div class="cal-day cal-blank" aria-hidden="true"></div>' for unused in range(trailing))
    task_count = sum(kind == "task" for entries in grouped.values() for stamp, kind, item in entries)
    event_count = sum(kind == "event" for entries in grouped.values() for stamp, kind, item in entries)
    weekdays = ''.join('<span' + (' class="cal-sunday"' if i == 0 else '') + '>' + name + '</span>'
                       for i, name in enumerate("일월화수목금토"))
    month_header = ('<div class="cal-month-head"><div><p class="eyebrow">이번 달을 한눈에 봐요</p>'
                    '<h2>' + str(first.year) + '년 <strong>' + str(first.month) + '월</strong></h2></div>'
                    '<span class="pill cal-today-label">오늘 ' + str(local_now.month) + '/' + str(local_now.day) + '</span></div>')
    legend = ('<div class="cal-legend"><span><i class="cal-legend-todo"></i>과제 ' + str(task_count) + '개</span>'
              '<span><i class="cal-legend-done"></i>낸 것</span><span><i class="cal-legend-event"></i>일정 ' + str(event_count) + '개</span></div>')
    calendar = ('<section class="card cal-board">' + month_header + legend + '<div class="cal-weekdays">' + weekdays + '</div>'
                '<div class="cal-grid" aria-label="' + str(first.month) + '월 달력">' + ''.join(cells) + '</div>'
                '<p class="muted cal-grid-note">작은 글씨는 아래 날짜별 목록에서 편하게 봐요.</p></section>')
    agenda = []
    for day in sorted(grouped):
        rows = ''.join(_task_row(item, now) if kind == "task" else event_row(item)
                       for stamp, kind, item in grouped[day])
        today_badge = '<span class="pill cal-today-label">오늘</span>' if day == local_now.day else ""
        agenda.append('<section class="cal-agenda-day"><div class="cal-agenda-date"><h3>'
                      + _e(_date(first.replace(day=day).isoformat(), time=False)) + '</h3>' + today_badge
                      + '</div><ul class="task-list">' + rows + '</ul></section>')
    agenda_html = ''.join(agenda) if agenda else _empty("이번 달에 잡힌 마감과 일정이 없어요.")
    body = calendar + _section("날짜별로 봐요", agenda_html)
    if undated:
        rows = ''.join(_task_row(item, now) if kind == "task" else event_row(item) for kind, item in undated)
        body += _section("날짜가 아직 없어요", '<ul class="task-list">' + rows + '</ul>')
    css = """
.cal-board{padding:22px 0 0;overflow:hidden}.cal-month-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 22px}.cal-month-head h2{font-size:22px;font-weight:500;margin:5px 0 0;letter-spacing:-.7px}.cal-month-head h2 strong{font-size:31px;font-weight:800}.cal-today-label{color:var(--blue);background:var(--sky);white-space:nowrap}.cal-legend{display:flex;flex-wrap:wrap;gap:16px;padding:18px 22px;color:var(--muted);font-size:12px}.cal-legend>span{display:flex;align-items:center;gap:6px}.cal-legend i{width:8px;height:8px;border-radius:2px;background:var(--ink)}.cal-legend .cal-legend-done{background:var(--green)}.cal-legend .cal-legend-event{background:var(--blue);border-radius:50%}.cal-weekdays,.cal-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}.cal-weekdays{background:var(--bg);border-top:1px solid var(--line);border-bottom:1px solid var(--line);text-align:center;padding:9px 0;font-size:11px;font-weight:700;color:var(--muted)}.cal-sunday{color:var(--crimson)}.cal-day{min-width:0;min-height:92px;padding:5px 3px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.cal-day:nth-child(7n){border-right:0}.cal-blank{background:var(--bg)}.cal-date{display:flex;width:26px;height:26px;align-items:center;justify-content:center;margin:0 auto 4px;font-size:12px;font-weight:600}.cal-today .cal-date{border:2px solid var(--blue);border-radius:50%;color:var(--blue);font-weight:800}.cal-chips{display:grid;gap:4px}.cal-chip{position:relative;display:flex;align-items:center;gap:4px;min-height:40px;padding:3px 4px;border-radius:5px;font-size:11px;line-height:1.25;min-width:0;background:var(--bg)}.cal-chip-title{min-width:0;flex:1;overflow:hidden}.cal-chip-title a,.cal-chip-title>span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:inherit;font-weight:600}.cal-chip-title a{min-height:40px;align-content:center;margin:-3px -4px -3px 0;padding:3px 4px 3px 0}.cal-chip-title a:after{content:"";position:absolute;inset:0;border-radius:5px}.cal-check{display:flex;flex:none;align-items:center;justify-content:center;width:10px;height:10px;border:1px solid currentColor;border-radius:3px;font-size:8px;font-weight:800}.cal-event{color:var(--blue);background:var(--sky)}.cal-event .cal-check{border:0;font-size:13px}.cal-done{color:var(--green);background:var(--mint)}.cal-missing{color:var(--crimson);background:var(--rose)}.cal-grid-note{padding:0 18px 13px;font-size:11px;margin:12px 0 0}.cal-agenda-day{margin-bottom:22px}.cal-agenda-date{display:flex;align-items:center;gap:10px;margin:0 0 10px}.cal-agenda-date h3{font-size:14px;margin:0}.cal-event-row{display:flex;align-items:flex-start;gap:14px}.cal-event-symbol{width:28px;height:28px;display:flex;align-items:center;justify-content:center;flex:none;border-radius:9px;color:var(--blue);background:var(--sky);font-size:22px}.cal-event-copy{min-width:0;flex:1}.cal-event-label{color:var(--blue);background:var(--sky)}
@media(min-width:650px){.cal-month-head h2{font-size:25px}.cal-month-head h2 strong{font-size:36px}.cal-day{min-height:120px;padding:8px 7px}.cal-date{margin:0 0 6px auto}.cal-chip{font-size:12px;padding:3px 6px;gap:6px}.cal-check{width:12px;height:12px;font-size:9px}.cal-weekdays{font-size:12px}.cal-grid-note{padding-bottom:17px}.cal-agenda-day .task-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
@media(max-width:420px){.cal-day{padding:4px 1px;min-height:85px}.cal-chip{gap:2px;padding:3px 2px;font-size:10px}.cal-check{width:9px;height:9px}.cal-legend{gap:12px;padding-left:18px;padding-right:18px}.cal-month-head{padding:0 18px}.cal-month-head h2{font-size:18px}.cal-month-head h2 strong{font-size:29px}}
.cal-chip{display:block;padding:0}.cal-check{position:absolute;left:3px;top:15px;z-index:1;pointer-events:none}.cal-chip-title{display:block}.cal-chip-title a,.cal-chip-title>span{display:block;min-width:40px;min-height:40px;margin:0;padding:6px 2px 6px 15px;line-height:28px}.cal-chip-title a:after{content:none}
"""
    return _page("calendar", data, now, body, css=css, wide=True)


def render_week(data: dict, now: datetime) -> str:
    """Monday–Sunday submission progress; excused tasks never enter counts."""
    local_now = now.astimezone(KST) if now.tzinfo else now.replace(tzinfo=KST)
    start = (local_now - timedelta(days=local_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    rows = []
    for item in _items(data, "tasks"):
        due = _dt(item.get("due_at"))
        if due and start <= due < end and item.get("status") != "excused":
            rows.append((due, item))
    rows.sort(key=lambda row: row[0])
    done_statuses = ("submitted", "graded", "late")
    completed = [item for due, item in rows if item.get("status") in done_statuses]
    remaining = [item for due, item in rows if item.get("status") not in done_statuses]
    total = len(rows)
    count = len(completed)
    percent = round(count / total * 100) if total else 0
    day_dots = []
    for offset, name in enumerate("월화수목금토일"):
        date = start + timedelta(days=offset)
        due_today = [item for due, item in rows if due.date() == date.date()]
        n_done = sum(item.get("status") in done_statuses for item in due_today)
        all_done = bool(due_today) and n_done == len(due_today)
        today = date.date() == local_now.date()
        classes = "week-day" + (" week-day-done" if all_done else "") + (" week-day-today" if today else "")
        mark = "✓" if all_done else str(date.day)
        detail = str(n_done) + "/" + str(len(due_today)) if due_today else "—"
        label = _date(date.isoformat(), time=False) + (", 마감 없음" if not due_today else ", " + str(len(due_today)) + "개 중 " + str(n_done) + "개 냄")
        day_dots.append('<div class="' + classes + '" aria-label="' + _e(label) + '"><span class="week-day-name">'
                        + name + '</span><span class="week-dot" aria-hidden="true">' + mark + '</span>'
                        '<span class="week-day-count">' + detail + '</span></div>')
    if not total:
        encouragement = "이번 주에 마감인 과제가 없어요. 잠깐 숨을 골라도 좋아요."
    elif not remaining:
        encouragement = "이번 주 마감을 모두 챙겼어요. 수고했어요!"
    else:
        encouragement = str(len(remaining)) + "개만 더 챙기면 이번 주 마감이 끝나요."
    date_range = _date(start.isoformat(), time=False) + "–" + _date((end - timedelta(days=1)).isoformat(), time=False)
    hero = ('<section class="card week-hero"><p class="eyebrow">' + _e(date_range) + '</p>'
            '<div class="week-progress-head"><h2><strong>' + str(count) + '</strong><span> / ' + str(total) + ' 냄</span></h2>'
            '<span class="pill week-percent">' + str(percent) + '%</span></div>'
            '<div class="week-bar" role="progressbar" aria-label="이번 주 제출률" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'
            + str(percent) + '"><span style="width:' + str(percent) + '%"></span></div>'
            '<p class="week-encouragement">' + encouragement + '</p><div class="week-days">' + ''.join(day_dots) + '</div>'
            '<p class="week-key muted">초록 체크는 그날 마감을 모두 냈다는 뜻이에요.</p></section>')
    remaining_html = '<ul class="task-list">' + ''.join(_task_row(item, now) for item in remaining) + '</ul>' if remaining else _empty("이번 주에 남은 과제가 없어요.")
    completed_html = '<ul class="task-list">' + ''.join(_task_row(item, now) for item in completed) + '</ul>' if completed else _empty("과제를 내면 여기에 모여요.")
    body = hero + _section("남은 것 " + str(len(remaining)) + "개", remaining_html)
    body += ('<details class="card week-completed"><summary><span>낸 것 <strong>' + str(count) + '개</strong></span>'
             '<span class="week-details-hint muted">펼쳐 보기</span></summary><div class="week-completed-content">' + completed_html + '</div></details>'
             '<p class="muted week-scope">월요일부터 일요일까지의 마감 기준이에요. 면제된 과제는 집계에서 뺐어요.</p>')
    css = """
.week-hero{padding:24px}.week-hero .eyebrow{margin-bottom:16px}.week-progress-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.week-progress-head h2{margin:0;line-height:1.15;letter-spacing:-1.8px;white-space:nowrap}.week-progress-head h2 strong{font-size:62px;font-weight:800;color:var(--green)}.week-progress-head h2>span{font-size:30px;font-weight:500;letter-spacing:-1px;color:var(--muted)}.week-percent{color:var(--green);background:var(--mint);font-size:15px;font-weight:800}.week-bar{height:24px;background:var(--bg);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:22px}.week-bar>span{height:100%;display:block;background:var(--green);border-radius:7px}.week-encouragement{font-size:13px;line-height:1.7;margin:14px 0 24px;color:var(--muted)}.week-days{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px;padding-top:20px;border-top:1px solid var(--line)}.week-day{display:flex;flex-direction:column;align-items:center;gap:9px;min-width:0}.week-day-name{font-size:12px;color:var(--muted);font-weight:600}.week-dot{display:flex;align-items:center;justify-content:center;width:34px;height:34px;border:1px solid var(--line);border-radius:50%;font-size:12px;font-weight:700;color:var(--muted)}.week-day-done .week-dot{color:var(--paper);background:var(--green);border-color:var(--green);font-size:18px}.week-day-today .week-dot{outline:2px solid var(--blue);outline-offset:3px}.week-day-today .week-day-name{color:var(--blue);font-weight:800}.week-day-count{font-size:11px;color:var(--muted)}.week-key{font-size:11px;text-align:center;margin:21px 0 0}.week-completed{padding:0;overflow:hidden;margin-top:24px}.week-completed summary{min-height:62px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:15px 20px;cursor:pointer;font-size:15px;font-weight:700;list-style:none}.week-completed summary::-webkit-details-marker{display:none}.week-completed summary:after{content:"+";font-size:22px;line-height:1;color:var(--muted)}.week-completed[open] summary:after{content:"−"}.week-details-hint{font-size:11px;font-weight:400;margin-left:auto}.week-completed[open] .week-details-hint{visibility:hidden}.week-completed-content{padding:0 12px 12px}.week-completed-content .card{background:var(--bg)}.week-scope{font-size:11px;line-height:1.8;margin:16px 2px 0}
@media(min-width:650px){.week-hero{padding:32px}.week-progress-head h2 strong{font-size:78px}.week-progress-head h2>span{font-size:36px}.week-dot{width:42px;height:42px;font-size:14px}.week-encouragement{font-size:14px}.week-key{font-size:12px}.week-days{gap:15px}}
@media(max-width:370px){.week-hero{padding:18px}.week-days{gap:3px}.week-dot{width:29px;height:29px}.week-progress-head h2 strong{font-size:55px}.week-progress-head h2>span{font-size:27px}}
"""
    return _page("week", data, now, body, css=css)


def _sq_matches_course(item, course):
    """Prefer Canvas ids, with a name fallback only when an id is unavailable."""
    item_id, course_id = item.get("course_id"), course.get("id")
    if item_id is not None and course_id is not None:
        return str(item_id) == str(course_id)
    return bool(item.get("course") and course.get("name")
                and str(item["course"]) == str(course["name"]))


def _sq_ratio(item):
    score, points = _number(item.get("score")), _number(item.get("points"))
    if score is None or points is None or points <= 0:
        return None
    return _number(score / points * 100)


def _sq_graded_at(item):
    return _dt(item.get("graded_at")) or _dt(item.get("due_at")) or datetime.min.replace(tzinfo=KST)


def _sq_sparkline(rows):
    usable = sorted((a for a in rows if _sq_ratio(a) is not None), key=_sq_graded_at)
    if not usable:
        return '<div class="sq-chart-empty muted">채점 기록이 쌓이면 보여요</div>'
    values = [_sq_ratio(a) for a in usable]
    ceiling = max(100, max(values))
    coords = [(8 + (224 * i / (len(values) - 1) if len(values) > 1 else 112),
               43 - 34 * max(0, value) / ceiling) for i, value in enumerate(values)]
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    dots = "".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.7"/>' for x, y in coords)
    return (f'<svg class="sq-sparkline" viewBox="0 0 240 52" role="img" '
            f'aria-label="채점된 과제 {len(values)}개의 점수 흐름">'
            '<path d="M8 43H232" class="sq-chart-baseline"/>'
            f'<polyline points="{points}"/>{dots}</svg>'
            f'<div class="sq-chart-caption muted">채점 기록 {len(values)}개</div>')


def render_scores(data: dict, now: datetime) -> str:
    courses, assignments = _items(data, "courses"), _items(data, "assignments")
    cards = []
    for course in courses:
        score = _number(course.get("score"))
        rows = [a for a in assignments if _sq_matches_course(a, course)]
        title_item = {"title": course.get("name") or "이름 없는 과목", "url": course.get("url")}
        grade = _e(course.get("grade")) if course.get("grade") not in (None, "") else ""
        metric = f'{score:.1f}<span>%</span>' if score is not None else '<span class="sq-no-score">점수 없음</span>'
        progress = max(0, min(100, score)) if score is not None else 0
        cards.append(
            '<article class="card sq-score-card">'
            f'<h2 class="sq-course-title">{_link(title_item)}</h2>'
            f'<div class="sq-score-line"><div class="sq-score">{metric}</div>'
            + (f'<span class="sq-grade">{grade}</span>' if grade else '') + '</div>'
            f'<div class="sq-score-bar" aria-hidden="true"><span style="width:{progress:.2f}%"></span></div>'
            f'{_sq_sparkline(rows)}</article>')
    graded = sorted((a for a in assignments if _number(a.get("score")) is not None
                     or _dt(a.get("graded_at")) is not None), key=_sq_graded_at, reverse=True)[:5]
    recent = []
    for item in graded:
        score, points, ratio = _number(item.get("score")), _number(item.get("points")), _sq_ratio(item)
        mark = f"{score:g}" if score is not None else "점수 대기"
        if score is not None and points is not None:
            mark += f" / {points:g}점"
        elif score is not None:
            mark += "점"
        ratio_text = f'<span class="sq-result-percent">{ratio:.1f}%</span>' if ratio is not None else ""
        recent.append('<li class="sq-result-row">'
                      f'<div class="sq-result-main"><span class="course">{_course(item)}</span>'
                      f'<div class="task-title">{_link(item)}</div>'
                      f'<div class="meta">{_date(item.get("graded_at") or item.get("due_at"))}</div></div>'
                      f'<div class="sq-result-score"><strong>{mark}</strong>{ratio_text}</div></li>')
    body = ('<div class="sq-toolbar"><p class="muted">점수와 쌓여 온 기록을 한눈에 봐요.</p>'
            '<button id="sq-privacy" type="button" hidden aria-pressed="false">숫자 가리기</button></div>')
    # The v2 hero shows the unweighted mean of available course scores.
    # Existing per-course scores, chart data, and privacy behavior stay unchanged.
    available_scores = [_number(course.get("score")) for course in courses]
    available_scores = [score for score in available_scores if score is not None]
    average = (f'{sum(available_scores) / len(available_scores):.1f}<span>%</span>'
               if available_scores else '<span>점수 없음</span>')
    overview = ('<section class="card score-overview"><div><p class="eyebrow">현재 점수 평균</p>'
                f'<div class="sq-average">{average}</div>'
                f'<p class="muted">점수가 있는 {len(available_scores)}개 과목의 단순 평균</p></div></section>')
    body = overview + body
    body += (f'<div class="sq-score-grid">{"".join(cards)}</div>' if cards
             else _empty("아직 표시할 과목이 없어요. 과목이 생기면 점수를 함께 볼 수 있어요."))
    body += _section("최근 채점", '<ul class="sq-results">' + "".join(recent) + '</ul>'
                     if recent else _empty("아직 채점 기록이 없어요. 결과가 올라오면 여기에 모아 드려요."))
    css = """
    .sq-toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 20px}
    .sq-toolbar p{margin:0;font-size:14px;line-height:1.5}.sq-toolbar button{flex:none;min-height:42px;padding:9px 12px;border:1px solid var(--line);border-radius:12px;background:var(--paper);color:var(--ink);font:inherit;font-size:13px;font-weight:700;cursor:pointer}
    .sq-score-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:28px}
    .sq-score-card.card{padding:16px;min-width:0}.sq-course-title{font-size:14px;line-height:1.45;margin:0 0 12px;min-height:41px;font-weight:700;overflow-wrap:anywhere}.sq-course-title a{color:var(--ink);display:block;min-height:41px}
    .sq-score-line{display:flex;align-items:center;justify-content:space-between;gap:6px;flex-wrap:wrap}.sq-score{font-size:30px;font-weight:800;letter-spacing:-1.2px;font-variant-numeric:tabular-nums;line-height:1.2}.sq-score>span:not(.sq-number){font-size:14px;font-weight:600;margin-left:2px;letter-spacing:0}.sq-score>.sq-no-score{font-size:22px;margin:0;font-weight:750}.sq-grade{font-size:12px;color:var(--blue);font-weight:750;white-space:nowrap}.sq-score-bar{height:4px;border-radius:9px;background:var(--line);margin:13px 0 8px;overflow:hidden}.sq-score-bar>span{height:100%;background:var(--blue);display:block;border-radius:9px}
    .sq-sparkline{display:block;width:100%;height:48px;overflow:visible}.sq-sparkline polyline{fill:none;stroke:var(--blue);stroke-width:2.4;stroke-linejoin:round;stroke-linecap:round}.sq-sparkline circle{fill:var(--blue)}.sq-chart-baseline{stroke:var(--line);stroke-width:1;fill:none}.sq-chart-caption{font-size:11px;margin-top:2px}.sq-chart-empty{display:flex;align-items:center;font-size:11px;line-height:1.5;height:66px}
    .sq-results{list-style:none;padding:0;margin:0}.sq-result-row{display:flex;gap:14px;padding:17px 0;align-items:center;border-bottom:1px solid var(--line)}.sq-result-row:last-child{border-bottom:0}.sq-result-main{flex:1;min-width:0}.sq-result-main .task-title a{display:inline-flex;align-items:center;min-height:40px}.sq-result-score{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex:none;font-size:13px;font-variant-numeric:tabular-nums}.sq-result-percent{font-size:12px;color:var(--muted)}.numbers-hidden .sq-number{filter:blur(6px);user-select:none}
    @media(min-width:700px){.sq-score-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.sq-score-card.card{padding:20px}.sq-score{font-size:34px}}
    @media(max-width:699px){.sq-course-title{font-size:13px;text-wrap:balance}}
    @media(max-width:350px){.sq-score-grid{grid-template-columns:1fr}.sq-course-title{min-height:0}.sq-toolbar{align-items:flex-start}}
    """
    js = """
    (function(){
      var button=document.getElementById('sq-privacy');
      if(!button)return;
      var walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,{acceptNode:function(node){
        return /[0-9]/.test(node.nodeValue)&&!node.parentElement.closest('script,style,noscript,svg,.sq-number')?NodeFilter.FILTER_ACCEPT:NodeFilter.FILTER_REJECT;
      }}),nodes=[],node;
      while((node=walker.nextNode()))nodes.push(node);
      nodes.forEach(function(text){var fragment=document.createDocumentFragment();text.nodeValue.split(/([0-9]+(?:[.,][0-9]+)*)/g).forEach(function(part){if(!part)return;if(/^[0-9]/.test(part)){var span=document.createElement('span');span.className='sq-number';span.textContent=part;fragment.appendChild(span);}else fragment.appendChild(document.createTextNode(part));});text.parentNode.replaceChild(fragment,text);});
      button.hidden=false;
      button.addEventListener('click',function(){var hidden=document.body.classList.toggle('numbers-hidden');button.setAttribute('aria-pressed',String(hidden));button.textContent=hidden?'숫자 다시 보기':'숫자 가리기';});
    })();
    """
    return _page("scores", data, now, body, css, js)


def _sq_xp(item):
    if _dt(item.get("submitted_at")) is None:
        return 0
    ratio = _sq_ratio(item)
    return 10 + (5 if item.get("late") is False else 0) + (5 if ratio is not None and ratio >= 90 else 0)


def _sq_missing_count(tasks, assignments):
    """Only explicit ids establish duplicates between the planner and assignments."""
    missing = set()
    for index, item in enumerate(tasks):
        if item.get("status") != "missing":
            continue
        raw = item.get("id")
        key = str(raw) if raw is not None else f"unknown-task-{index}"
        missing.add((str(item.get("course_id")), key))
    for index, item in enumerate(assignments):
        if item.get("missing") is not True:
            continue
        raw = item.get("id")
        key = "assignment_" + str(raw) if raw is not None else f"unknown-assignment-{index}"
        missing.add((str(item.get("course_id")), key))
    return len(missing)


def _sq_tiger():
    # An original geometric cub, drawn here without image assets or external requests.
    return '''<svg class="sq-tiger" viewBox="0 0 200 180" role="img" aria-label="함께 자라는 아기 호랑이">
    <ellipse cx="100" cy="163" rx="60" ry="8" fill="currentColor" opacity=".09"/>
    <path d="M137 140Q182 146 170 112" fill="none" stroke="#E5A448" stroke-width="15" stroke-linecap="round"/>
    <path d="M171 121L162 124M173 135L164 132" fill="none" stroke="#6B422A" stroke-width="6"/>
    <ellipse cx="101" cy="127" rx="41" ry="37" fill="#F2B95D"/>
    <ellipse cx="101" cy="134" rx="24" ry="25" fill="#FFF0CB"/>
    <ellipse cx="75" cy="156" rx="18" ry="10" fill="#E5A448"/>
    <ellipse cx="126" cy="156" rx="18" ry="10" fill="#E5A448"/>
    <circle cx="61" cy="48" r="20" fill="#E5A448"/><circle cx="139" cy="48" r="20" fill="#E5A448"/>
    <circle cx="61" cy="48" r="10" fill="#FFF0CB"/><circle cx="139" cy="48" r="10" fill="#FFF0CB"/>
    <rect x="48" y="36" width="104" height="84" rx="39" fill="#F2B95D"/>
    <path d="M88 38L94 55M112 38L106 55M51 68L66 73M50 87L65 85M149 68L134 73M150 87L135 85" stroke="#6B422A" stroke-width="6" stroke-linecap="round"/>
    <ellipse cx="87" cy="95" rx="18" ry="15" fill="#FFF0CB"/><ellipse cx="113" cy="95" rx="18" ry="15" fill="#FFF0CB"/>
    <circle cx="79" cy="79" r="4.5" fill="#382C2A"/><circle cx="121" cy="79" r="4.5" fill="#382C2A"/>
    <circle cx="80" cy="78" r="1.3" fill="white"/><circle cx="122" cy="78" r="1.3" fill="white"/>
    <path d="M94 90Q100 85 106 90L100 96Z" fill="#6B422A"/>
    <path d="M100 96V101M100 101Q93 107 88 101M100 101Q107 107 112 101" fill="none" stroke="#6B422A" stroke-width="2.8" stroke-linecap="round"/>
    <path d="M64 124L72 128M138 124L130 128" stroke="#6B422A" stroke-width="5" stroke-linecap="round"/>
    </svg>'''


def render_quest(data: dict, now: datetime) -> str:
    now = _dt(now)
    tasks, assignments = _items(data, "tasks"), _items(data, "assignments")
    submissions = sorted((a for a in assignments if _dt(a.get("submitted_at")) is not None),
                         key=lambda a: _dt(a.get("submitted_at")), reverse=True)
    xp = sum(_sq_xp(a) for a in submissions)
    level, progress = 1 + xp // 100, xp % 100
    missing = _sq_missing_count(tasks, assignments)
    hp = max(0, 100 - 15 * missing)
    quests = sorted((t for t in tasks if not _done(t) and _dt(t.get("due_at")) is not None
                     and now <= _dt(t.get("due_at")) <= now + timedelta(days=7)),
                    key=lambda t: _dt(t.get("due_at")))
    quest_rows = []
    for item in quests:
        quest_rows.append('<li class="sq-quest-row">'
                          '<span class="sq-quest-check" aria-hidden="true"></span>'
                          f'<div class="sq-quest-main"><span class="course">{_course(item)}</span>'
                          f'<div class="task-title">{_link(item)}</div>'
                          f'<div class="meta">{_e(_due_label(item.get("due_at"), now))} · {_date(item.get("due_at"))}</div></div>'
                          '<span class="sq-xp-reward">+15 XP</span></li>')
    history = []
    for item in submissions[:5]:
        history.append('<li class="sq-quest-row sq-history-row">'
                       '<span class="sq-history-check" aria-hidden="true">✓</span>'
                       f'<div class="sq-quest-main"><span class="course">{_course(item)}</span>'
                       f'<div class="task-title">{_link(item)}</div>'
                       f'<div class="meta">{_date(item.get("submitted_at"))} 제출</div></div>'
                       f'<span class="sq-xp-reward">+{_sq_xp(item)} XP</span></li>')
    body = ('<p class="sq-quest-intro muted">과제 하나씩, 호랑이와 함께 자라요.</p>'
            '<section class="card sq-hero" aria-label="나의 학습 캐릭터">'
            f'<div class="sq-character">{_sq_tiger()}<span class="sq-level">LEVEL {level}</span></div>'
            '<div class="sq-character-stats"><p class="eyebrow">오늘도 한 걸음</p><h2>차곡차곡 아기 호랑이</h2>'
            f'<div class="sq-stat-label"><span>경험치</span><strong>{xp} XP</strong></div>'
            f'<div class="sq-game-bar" role="progressbar" aria-label="다음 레벨까지 경험치" aria-valuenow="{progress}" aria-valuemin="0" aria-valuemax="100"><span style="width:{progress}%"></span></div>'
            f'<p class="sq-next-level muted">다음 레벨까지 {100 - progress} XP 남았어요</p>'
            f'<div class="sq-stat-label"><span>HP</span><strong>{hp} / 100</strong></div>'
            f'<div class="sq-game-bar sq-hp-bar" role="progressbar" aria-label="HP" aria-valuenow="{hp}" aria-valuemin="0" aria-valuemax="100"><span style="width:{hp}%"></span></div>'
            f'<p class="sq-next-level muted">미제출 {missing}개 · 하나당 HP −15</p></div></section>'
            f'<section class="sq-quests"><div class="section-head"><h2>퀘스트</h2><span class="pill">7일 안에 {len(quests)}개</span></div>'
            '<p class="muted sq-quest-help">기한 안에 내면 경험치를 받아요.</p>'
            + ('<ul class="sq-quest-list">' + "".join(quest_rows) + '</ul>' if quest_rows
               else _empty("7일 안에 남은 퀘스트가 없어요. 잠깐 쉬어 가도 좋아요."))
            + '</section>'
            + _section("최근 기록", '<ul class="sq-quest-list">' + "".join(history) + '</ul>'
                       if history else _empty("아직 제출 기록이 없어요. 첫 과제가 모험의 시작이에요."))
            + '<details class="sq-game-rules"><summary>경험치와 HP는 어떻게 계산해요?</summary>'
            '<p>제출하면 10 XP, 지각하지 않은 제출이면 5 XP를 더 받아요. 채점 점수가 만점의 90% 이상이면 5 XP를 더 받아요. 100 XP마다 한 레벨씩 올라요.</p>'
            '<p>HP는 100에서 미제출 과제당 15씩 줄고, 가장 낮은 값은 0이에요. 같은 과제 ID는 한 번만 세어요. 화면을 열 때 받은 LMS 기록으로 다시 계산해요.</p></details>')
    css = """
    .sq-quests .section-head h2{margin:0;font-size:17px;line-height:1.5}
    .sq-quest-intro{font-size:14px;margin:0 0 20px}.sq-hero.card{background:var(--mint);display:grid;grid-template-columns:136px minmax(0,1fr);gap:14px;padding:22px 18px;margin-bottom:28px;align-items:center;border-color:transparent}.sq-character{text-align:center}.sq-tiger{display:block;width:100%;height:auto;color:var(--ink)}.sq-level{display:inline-block;background:var(--paper);color:var(--green);border:1px solid var(--line);border-radius:99px;font-weight:800;font-size:11px;letter-spacing:1px;padding:6px 12px;margin-top:4px}.sq-character-stats{min-width:0}.sq-character-stats .eyebrow{font-size:10px;letter-spacing:.04em;margin:0 0 6px;color:var(--green)}.sq-character-stats h2{font-size:17px;line-height:1.45;letter-spacing:-.4px;margin:0 0 18px;word-break:keep-all}.sq-stat-label{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-bottom:7px}.sq-stat-label strong{font-variant-numeric:tabular-nums}.sq-game-bar{height:8px;overflow:hidden;background:var(--paper);border-radius:99px}.sq-game-bar>span{display:block;background:var(--green);height:100%;border-radius:99px}.sq-hp-bar>span{background:var(--crimson)}.sq-next-level{font-size:10px;line-height:1.5;margin:6px 0 14px}.sq-next-level:last-child{margin-bottom:0}.sq-quest-help{font-size:13px;margin:0 0 10px}.sq-quests{margin-bottom:28px}.sq-quest-list{padding:0;margin:0;list-style:none}.sq-quest-row{display:flex;align-items:center;gap:12px;padding:17px 0;border-bottom:1px solid var(--line)}.sq-quest-row:last-child{border-bottom:0}.sq-quest-main{flex:1;min-width:0}.sq-quest-main .task-title a{display:inline-flex;align-items:center;min-height:40px}.sq-quest-check{display:block;width:17px;height:17px;border:1.5px solid var(--line);border-radius:5px;flex:none;background:var(--paper)}.sq-xp-reward{font-size:11px;font-weight:800;white-space:nowrap;flex:none;padding:6px 8px;border-radius:8px;color:var(--green);background:var(--mint)}.sq-history-check{flex:none;color:var(--green);font-size:16px;font-weight:800;width:17px}.sq-game-rules{border-top:1px solid var(--line);padding-top:12px;font-size:12px;color:var(--muted);margin-top:25px}.sq-game-rules summary{min-height:42px;display:list-item;align-content:center;cursor:pointer}.sq-game-rules p{line-height:1.8}.sq-quest-row .meta{font-size:11px;line-height:1.5}
    @media(min-width:600px){.sq-hero.card{grid-template-columns:200px minmax(0,1fr);gap:36px;padding:28px 36px}.sq-character-stats h2{font-size:23px}.sq-stat-label{font-size:14px}.sq-next-level{font-size:12px}.sq-game-bar{height:10px}.sq-level{font-size:12px}.sq-xp-reward{font-size:12px;padding:8px 11px}}
    @media(max-width:350px){.sq-hero.card{grid-template-columns:105px minmax(0,1fr);gap:10px;padding:18px 12px}.sq-character-stats h2{font-size:15px}.sq-quest-row{gap:8px}.sq-xp-reward{padding:5px;font-size:10px}}
    """
    return _page("quest", data, now, body, css)


# ==== VIEWS END ====


# ───────────────────────── 실행 ─────────────────────────

def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    ap = argparse.ArgumentParser(description="고려대 LMS 데이터로 대시보드 한 장 만들기 (조회만)")
    ap.add_argument("--ui", type=int, choices=sorted(UIS), help="화면 번호 1–8")
    ap.add_argument("--sample", action="store_true", help="토큰 없이 가짜 데이터로")
    ap.add_argument("--list", action="store_true", help="화면 목록 보기")
    ap.add_argument("--out", help="결과 파일 경로 (기본: dashboard-<번호>-<이름>.html)")
    ap.add_argument("--save-data", action="store_true", help="받은 데이터를 data.json 으로도 저장")
    a = ap.parse_args(argv)

    if a.list or not a.ui:
        for n, (slug, title, _) in UIS.items():
            print(f"{n}. {title}  (--ui {n})")
        return 0

    slug, title, needs = UIS[a.ui]
    now = now_kst()
    if a.sample:
        data = sample(now)
    else:
        token = os.environ.get("CANVAS_TOKEN", "").strip()
        if not token:
            print("CANVAS_TOKEN 이 비어 있어요. 토큰을 환경변수로 넣거나, --sample 로 가짜 데이터로 해 봐요.",
                  file=sys.stderr)
            return 2
        try:
            data = fetch(needs, token, now)
        except LmsError as e:
            print(f"LMS 오류: {e}", file=sys.stderr)
            return 1

    render = globals().get(f"render_{slug}")
    if render is None:
        print(f"아직 {title} 화면이 없어요.", file=sys.stderr)
        return 3
    out = render(data, now)
    ext = "md" if slug == "briefing" else "html"
    path = Path(a.out or f"dashboard-{a.ui}-{slug}.{ext}")
    path.write_text(out, encoding="utf-8")
    if a.save_data:
        Path("data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    print(f"{a.ui}. {title} → {path}  ({'가짜 데이터' if a.sample else 'LMS'}: "
          + ", ".join(f"{k} {v}" for k, v in counts.items()) + ")")
    if slug == "briefing":
        print()
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
