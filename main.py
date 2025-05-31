#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel

import requests
import yaml
import json

from dataclasses import dataclass

from lib.utils import *
from lib.logger import *

import click

# アプリとBotトークンを環境変数から読み込む
app_token = os.getenv("SLACK_APP_TOKEN")
bot_token = os.getenv("SLACK_BOT_TOKEN")

app = App(token=bot_token)
client = WebClient(token=bot_token)


class Linear:
    def __init__(self, api_url, api_key):
        self.api_url = api_url
        self.api_key = api_key
        self.headers = {
            "Authorization": f"{self.api_key}",
            "Content-Type": "application/json",
        }

    def get_uuid_for_team(self, team_name):
        """
        チーム名からUUIDを取得するメソッド
        """
        query = """
        query($teamName: String!) {
            teams(filter: {name: {eq: $teamName}}) {
                nodes {
                    id
                    name
                }
            }
        }
        """
        variables = {"teamName": team_name}
        data = {"query": query, "variables": variables}

        response = requests.post(self.api_url, json=data, headers=self.headers)

        if response.status_code == 200:
            response_data = response.json()
            if "data" in response_data and "teams" in response_data["data"]:
                teams = response_data["data"]["teams"]["nodes"]
                if teams:
                    return teams[0]["id"]

    def get_state_id_by_name(self, state_name):
        """
        Linear APIを使って、指定された状態名（state_name）に対応するstateId（UUID）を取得する
        """
        query = """
        query {
          workflowStates {
            nodes {
              id
              name
            }
          }
        }
        """
        data = {"query": query}
        response = requests.post(self.api_url, json=data, headers=self.headers)

        if response.status_code == 200:
            response_data = response.json()
            states = (
                response_data.get("data", {}).get("workflowStates", []).get("nodes", [])
            )

            # state_nameに一致する状態のIDを探す
            for state in states:
                if (
                    state["name"].lower() == state_name.lower()
                ):  # 大文字小文字を区別せず検索
                    return state["id"]

            logger.error(f"State '{state_name}' not found in the response.")
            return None
        else:
            logger.error(
                f"Failed to fetch states: {response.status_code} - {response.text}"
            )
            return None

    def create_issue(self, team_id, title, description):
        """
        指定されたチームIDで新しいIssueを作成するメソッド
        state_id（状態ID）を指定して発行する
        """
        mutation = """
        mutation($teamId: String!, $title: String!, $description: String!, $stateId: String!) {
            issueCreate(input: {
                teamId: $teamId
                title: $title
                description: $description
            }) {
                issue {
                    id
                    title
                    description
                }
            }
        }
        """

        state_id = self.get_state_id_by_name(state_name)
        variables = {"teamId": team_id, "title": title, "description": description}
        data = {"query": mutation, "variables": variables}

        response = requests.post(self.api_url, json=data, headers=self.headers)

        if response.status_code == 200:
            response_data = response.json()
            if (
                response_data
                and "data" in response_data
                and "issueCreate" in response_data["data"]
            ):
                return response_data["data"]["issueCreate"]["issue"]
            else:
                logger.error(
                    "Error: Invalid response data or missing 'data' or 'issueCreate'."
                )
                return None
        else:
            logger.error(
                f"Failed to create issue: {response.status_code} - {response.text}"
            )
            return None


@dataclass
class Issue:
    id: str
    title: str
    description: str

    def __str__(self):
        return (
            f"Issue(id={self.id}, title={self.title}, description={self.description})"
        )


def llm(body) -> Issue | None:
    model = GeminiModel(
        config["system"]["llm"]["model"],
    )

    # Pydantic AIの設定
    agent = Agent(
        model,
        system_prompt="渡された文字列からGitHubのIssueのタイトルを50文字にまとめて生成します。",
        result_type=str,
        max_tokens=config["system"]["llm"]["max_tokens"],
        temperature=config["system"]["llm"]["temperature"],
    )

    logger.info(f"Received body: {body}")
    result = agent.run_sync(body)
    logger.info(f"Generating issue title with body: {result}")

    issue = Issue(
        id="",
        title=result.output,
        description=body,
    )

    # `result` は AgentRunResult オブジェクトなので、output属性を文字列として取り出す
    return issue


@app.event("app_mention")
def healthcheck(body, say):
    """アプリがメンションされた時のヘルスチェック"""
    mention = body["event"]
    if "ping" in mention["text"].lower():
        text = "pong"
        channel = mention["channel"]
        thread_ts = mention["ts"]
        say(text=text, channel=channel, thread_ts=thread_ts)
    elif "config" in mention["text"].lower():
        text = "現在の設定は以下の通りです:\n"
        for k, v in config.items():
            text += f"{k}: {v}\n"
        channel = mention["channel"]
        thread_ts = mention["ts"]
        say(text=text, channel=channel, thread_ts=thread_ts)
    elif "help" in mention["text"].lower():
        text = "このボットは、特定のリアクションが付いたメッセージに対して、LinearでIssueを作成します。"
        text += "\nリアクションの設定はconfig.yamlで行います。"
        text += "\n\nhelp: このメッセージを表示します"
        text += "\nping: ボットの応答を確認します"
        text += "\nconfig: 現在の設定を表示します"
        channel = mention["channel"]
        thread_ts = mention["ts"]
        say(text=text, channel=channel, thread_ts=thread_ts)
    else:
        pass


# 特定のリアクションが付いた時にスレッドで返信するリスナー
@app.event("reaction_added")
def reaction_handler(body, say):
    reaction = body["event"]
    item = reaction["item"]
    channel = item["channel"]
    thread_ts = item.get("ts", None)
    reaction_name = reaction["reaction"]
    user = reaction["user"]
    timestamp = reaction["event_ts"]
    message_ts = item["ts"]

    logger.debug(f"Reaction added: {reaction_name} by user {user} in channel {channel}")

    for k, v in config.items():
        if k != "reaction_mentions":
            continue
        for value in v:
            if value["reaction"] == reaction_name and value["mention"] == f"<@{user}>":
                logger.info(f"マッチしたリアクション: {reaction_name}")
                text = f"{value['mention']} やります！"
                say(text=text, channel=channel, thread_ts=thread_ts)

                try:
                    # conversations_history を使ってメッセージを取得
                    response = client.conversations_history(
                        channel=channel, latest=message_ts, limit=1, inclusive=True
                    )

                    # レスポンスにメッセージが含まれているかを確認
                    messages = response.get("messages", [])
                    if not messages:
                        logger.warning("No messages found for this timestamp.")
                        return  # メッセージがない場合は処理を終了

                    # メッセージのテキストを取得
                    message = messages[0]
                    message_text = message["text"]

                    issue = llm(message_text)
                    if not issue:
                        say(
                            "Issueのタイトルを生成できませんでした。",
                            channel=channel,
                            thread_ts=thread_ts,
                        )
                        logger.error("Issueのタイトルを生成できませんでした。")
                        return

                    title = issue.title
                    description = issue.description

                    # Issueを作成
                    if os.getenv("DEBUG") == "true":
                        logger.debug(f"Creating issue with title: {title}")
                        pass
                    else:
                        issue = linear.create_issue(
                            team_id=linear.get_uuid_for_team(value["team_id"]),
                            title=title,
                            description=description,
                        )
                        if issue:
                            say(
                                f"新しいIssueが作成されました: {issue['title']} (URL: https://linear.app/ivry/issue/{issue['id']})",
                                channel=channel,
                                thread_ts=thread_ts,
                            )
                            logger.info(
                                f"Issueが作成されました: {issue['title']} (ID: {issue['id']})"
                            )
                        else:
                            say(
                                "Issueの作成に失敗しました。",
                                channel=channel,
                                thread_ts=thread_ts,
                            )
                            logger.error("Issueの作成に失敗しました。")
                except Exception as e:
                    logger.error(f"Error retrieving message: {e}")
                    say(
                        "メッセージの取得に失敗しました。",
                        channel=channel,
                        thread_ts=thread_ts,
                    )
                    return
                break
        else:
            continue


@click.command()
@click.option("--debug", is_flag=True, help="Enable debug mode")
def main(debug):
    if debug:
        os.environ["DEBUG"] = "true"
        logger.setLevel("DEBUG")
        logger.info("Debug mode is enabled.")
    else:
        os.environ["DEBUG"] = "false"
        logger.setLevel("INFO")
        logger.info("Debug mode is disabled.")
    logger.debug(f"Configuration loaded: {json.dumps(config, indent=2)}")

    logger.info("Starting the Slack bot...")
    handler = SocketModeHandler(app, app_token)
    handler.start()
    logger.info("Slack bot is running.")


if __name__ == "__main__":
    linear = Linear(
        api_url="https://api.linear.app/graphql", api_key=os.getenv("LINEAR_API_KEY")
    )
    logger.info("Linear API initialized successfully.")

    config = load_config()
    main()
