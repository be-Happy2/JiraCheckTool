import asyncio
import platform
import yaml
import requests
import json
import logging
import jira
from time import time
from datetime import datetime, timedelta


# 配置日志输出的格式和级别
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    filename='log.log', filemode='w', level=logging.DEBUG)


# 单独定义项目数据类型
class JiraProject:
    name: str
    key: str
    issues: jira.client.ResultList  # 项目所有问题
    project_manager: str
    manager_phone: str

    def __init__(self, name: str, key: str, issues: jira.client.ResultList | None,
                 project_manager: str, manager_phone: str):
        self.name = name
        self.key = key
        self.issues = issues
        self.project_manager = project_manager
        self.manager_phone = manager_phone


# 用于加载配置信息
class JiraConfig:
    jira_server_url: str
    username: str
    password: str
    project_list: list[JiraProject]
    robot_url: str
    robot_tag: str
    time_range: int

    # config_file 为配置文件的路径
    def __init__(self, config_file: str):
        logging.info('Loading the configuration file ...')
        try:
            with open(config_file, 'r', encoding='utf-8') as file:
                yaml_data = yaml.safe_load(file)
            self.jira_server_url = yaml_data['jira_server_url']
            self.username = yaml_data['username']
            self.password = yaml_data['password']
            project_yaml_list = yaml_data['list_project']
            # 将字典类型转为JiraProject类型
            self.project_list = []
            for yaml_project in project_yaml_list:
                name = yaml_project['name']
                key = yaml_project['key']
                project_manager = yaml_project['project_manager']
                manager_phone = yaml_project['manager_phone']
                jira_project = JiraProject(name, key, None, project_manager, manager_phone)
                self.project_list.append(jira_project)
            self.robot_url = yaml_data['robot_url']
            self.robot_tag = yaml_data['robot_tag']
            self.time_range = yaml_data['time_range']
        except Exception as e:
            logging.exception(e)


# 查询项目jira
class JiraServer:
    # issue的状态，这里只列举两种状态
    CREATED = 'created'
    RESOLVED = 'resolutiondate'

    jira: jira.JIRA

    # 构造jira服务，需要连接至jira服务
    def __init__(self, url: str, username: str, password: str):
        logging.info('Connect to the Jira server ...')
        # 使用python jira模块来进行连接
        try:
            self.jira = jira.JIRA(server=url, basic_auth=(username, password))
        except Exception as e:
            logging.exception(e)

    # 根据 ResolutionDate 查询项目
    # 返回查询到的项目信息，包括该项目所有issue的所有属性
    async def query_project(self, project: JiraProject, start_date: str, end_date: str, query_type: str) -> JiraProject:
        jql = f'project = "{project.key}" and {query_type} >= "{start_date}" and {query_type} <= "{end_date}"'
        issues = self.jira.search_issues(jql)
        project.issues = issues
        return project

    # dict_project为一个dict[自定义项目名, jira项目名]
    async def async_query_projects(self, list_project: list[JiraProject],
                                   start_date: str, end_date: str, query_type: str) -> list:
        loop = asyncio.get_event_loop()
        tasks = []
        for project in list_project:
            # 使用run_in_executor()将IO耗时操作包装为协程函数执行，提高查询速度
            coroutine_task = await loop.run_in_executor(None, self.query_project,
                                                        project, start_date, end_date, query_type)
            task = asyncio.create_task(coroutine_task)
            tasks.append(task)
        result = await asyncio.gather(*tasks)
        return list(result)

    # 根据确定的日期查询多个项目
    def query_projects(self, list_project: list[JiraProject], start_date: str, end_date: str, query_type: str) -> list:
        # windows下需要开启此项, 必须在此位置声明
        if platform.system() == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return asyncio.run(self.async_query_projects(list_project, start_date, end_date, query_type))

    # 查询n天前到今天的多个项目
    def query_projects_from_days_ago(self, list_project: list[JiraProject], n: int, query_type: str) -> list:
        # 获取当前日期
        today_str = datetime.now().date().strftime('%Y-%m-%d')
        # 计算n天前的日期
        days_ago_str = (datetime.now().date() - timedelta(days=n)).strftime('%Y-%m-%d')
        return self.query_projects(list_project, days_ago_str, today_str, query_type)


# jira检查
class JiraCheckTool:
    """
    Jira中的字段有可能发生变化，如storypoints从 customfield_10002 变为了 customfield_10408
    若获取的值和实际不一致，请查看 issue.raw 分析对应字段的值
    """
    # 检查项目中存在问题的issue，返回一个字典，字典的键为issue编号，值为一个列表
    @staticmethod
    def get_project_problem(project: JiraProject) -> dict:
        unqualified_issue_dict = {}
        for issue in project.issues:
            unqualified_issue = JiraCheckTool.check_issue(issue)
            if len(unqualified_issue) != 0:
                unqualified_issue_dict[issue.key] = unqualified_issue
        return unqualified_issue_dict

    # 根据issue的类型来检查
    @staticmethod
    def check_issue(issue) -> list[str]:
        # issue.fields.issuetype 为 IssueType类型，所以不能直接判断，其属性.name才为str类型
        if issue.fields.issuetype.name == 'Story' or issue.fields.issuetype.name == '故事':
            return JiraCheckTool.check_story(issue)
        elif issue.fields.issuetype.name == 'Bug' or issue.fields.issuetype.name == '缺陷':
            return JiraCheckTool.check_bug(issue)
        elif issue.fields.issuetype.name == 'Review':
            return JiraCheckTool.check_review(issue)
        else:
            return []

    # 所有类型的jira issue都包含的公共问题
    @staticmethod
    def __common_problem(issue, err_list) -> list[str]:
        if issue.fields.duedate is None:
            err_list.append('No DudeDate')
        else:
            # 是否Delay
            due_date = datetime.strptime(issue.fields.duedate, '%Y-%m-%d')
            if issue.fields.resolutiondate is None:
                if datetime.now() > due_date:
                    err_list.append('Delay')
            else:
                resolution_date = datetime.strptime(issue.fields.resolutiondate, '%Y-%m-%dT%H:%M:%S.%f%z').date()
                if resolution_date > due_date.date():
                    err_list.append('Delay')
        if len(issue.fields.fixVersions) == 0:
            err_list.append('No FixVersions')
        # 检查components是否包含chengdu
        if 'chengdu' not in [item.name for item in issue.fields.components]:
            err_list.append('No Component Chengdu')
        if issue.fields.labels is None:
            err_list.append('No Labels')
        if issue.fields.customfield_11807 is None:
            err_list.append('No Implementer')
        if 'Askey-Secure' != issue.fields.security.name:
            err_list.append('No Askey-Secure')
        return err_list

    # 检查Story
    @staticmethod
    def check_story(issue) -> list[str]:
        err_list = []
        # 处理comment
        comment = ""
        for single_comment in issue.fields.comment.comments:
            comment += single_comment.body + "\n"
        comment = comment.lower()
        # 无编码
        if '[commitlink]' not in comment:
            err_list.append('No Code')
        # 无自验
        if ('[version]' not in comment) or ('[steps]' not in comment) or ('[result]' not in comment):
            err_list.append('No Verification')
        # StoryPoint大于3需要subtask
        if issue.fields.customfield_10408 is None:
            err_list.append('No StoryPoints')
            err_list.append('No SubTask')
        elif issue.fields.customfield_10408 > 3 and issue.fields.issuetype.subtask is False:
            err_list.append('No SubTask')
        return JiraCheckTool.__common_problem(issue, err_list)

    # 检查Bug项
    @staticmethod
    def check_bug(issue) -> list[str]:
        err_list = []
        # 处理comment
        comment = ""
        for single_comment in issue.fields.comment.comments:
            comment += single_comment.body + "\n"
        comment = comment.lower()
        # 无设计
        if '[analyse]' not in comment:
            err_list.append('No Analyse')
        # 无解决方案
        if '[solution]' not in comment:
            err_list.append('No Solution')
        # 无编码
        if '[commitlink]' not in comment:
            err_list.append('No Code')
        # 无自验
        if ('[version]' not in comment) or ('[steps]' not in comment) or ('[result]' not in comment):
            err_list.append('No Verification')
        # StoryPoint大于3需要subtask
        if issue.fields.customfield_10408 is None:
            err_list.append('No StoryPoints')
            err_list.append('No SubTask')
        elif issue.fields.customfield_10408 > 3 and issue.fields.issuetype.subtask is False:
            err_list.append('No SubTask')
        return JiraCheckTool.__common_problem(issue, err_list)

    # 检查Review项
    @staticmethod
    def check_review(issue) -> list[str]:
        err_list = []
        if issue.fields.customfield_10408 is None:
            err_list.append('No StoryPoints')
        return JiraCheckTool.__common_problem(issue, err_list)


# 用于发送通知的类
class DingDing:
    # 格式化issue list，将其转为可阅读的格式
    @staticmethod
    def generate_project_dingding_report_by_issue_dict(project_name: str, issue_dict: dict[str, list[str]],
                                                       tag: str) -> str:
        # 消息中必须包含在自定义机器人时添加标签，否则无法发送
        project_report = (f'---------- {tag} ----------\n'
                          f'{project_name}\n')
        for issue_key, issue_info in issue_dict.items():
            project_report += f'    {issue_key}: {issue_info}\n'
        return project_report

    @staticmethod
    def send_message(url: str, msg: str, project_manager: str, manager_phone: str) -> None:
        logging.info(f'Send Jira Report to DingDing @{project_manager}:{manager_phone}...')
        headers = {
            'Content-Type': 'application/json'
        }
        payload = {
            "msgtype": "text",
            "text": {
                "content": msg,
            },
            "at": {
                # 通过电话号码可以进行@
                "atMobiles": [manager_phone],
                "isAtAll": False
            }
        }
        try:
            requests.post(url, headers=headers, data=json.dumps(payload))
        except Exception as e:
            logging.exception(e)


def main():
    # 加载配置
    config = JiraConfig('./src/config.yaml')
    # 查询项目jira
    jira_server = JiraServer(config.jira_server_url, config.username, config.password)
    list_project = jira_server.query_projects_from_days_ago(config.project_list, config.time_range, JiraServer.CREATED)
    # 检查jira issue的不符合项
    for project in list_project:
        # 检查项目
        project_issue_dict = JiraCheckTool.get_project_problem(project)
        # 若检查到问题，将项目的检查结果通知到钉钉并@负责人
        if len(project_issue_dict) != 0:
            # 根据有问题的issue list生成项目报告
            report = DingDing.generate_project_dingding_report_by_issue_dict(project.name,
                                                                             project_issue_dict, config.robot_tag)
            # 将项目报告发送到钉钉并@
            DingDing.send_message(config.robot_url, report, project.project_manager, project.manager_phone)


if __name__ == '__main__':
    start = time()
    main()
    logging.debug(f'Script execution time : {round(time() - start, 3)}s')
