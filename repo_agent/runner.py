import threading
import os, json
import json
from repo_agent.file_handler import FileHandler
from repo_agent.change_detector import ChangeDetector
from repo_agent.project_manager import ProjectManager
from repo_agent.chat_engine import ChatEngine
from concurrent.futures import ThreadPoolExecutor
from repo_agent.doc_meta_info import MetaInfo, DocItem, DocItemType, DocItemStatus
from repo_agent.log import logger
from repo_agent.config import CONFIG
from repo_agent.multi_task_dispatch import worker
from tqdm import tqdm
from typing import List
from functools import partial
import subprocess
import shutil  



def need_to_generate(doc_item: DocItem, ignore_list: List) -> bool:
    """只生成item的，文件及更高粒度都跳过。另外如果属于一个blacklist的文件也跳过"""
    if doc_item.item_status == DocItemStatus.doc_up_to_date:
        return False
    rel_file_path = doc_item.get_full_name()
    if doc_item.item_type in [DocItemType._file, DocItemType._dir, DocItemType._repo]: #暂时不生成file及以上的doc
        return False
    doc_item = doc_item.father
    while doc_item:
        if doc_item.item_type == DocItemType._file:
            # 如果当前文件在忽略列表中，或者在忽略列表某个文件路径下，则跳过
            if any(
                rel_file_path.startswith(ignore_item) for ignore_item in ignore_list
            ):
                return False
            else:
                return True
        doc_item = doc_item.father
    return False


def load_whitelist():
    if CONFIG["whitelist_path"] != None:
        assert os.path.exists(
            CONFIG["whitelist_path"]
        ), f"whitelist_path must be a json-file,and must exists: {CONFIG['whitelist_path']}"
        with open(CONFIG["whitelist_path"], "r") as reader:
            white_list_json_data = json.load(reader)
        # for i in range(len(white_list_json_data)):
        #     white_list_json_data[i]["file_path"] = white_list_json_data[i]["file_path"].replace("https://github.com/huggingface/transformers/blob/v4.36.1/","")
        return white_list_json_data
    else:
        return None


class Runner:
    def __init__(self):
        self.project_manager = ProjectManager(
            repo_path=CONFIG["repo_path"], project_hierarchy=CONFIG["project_hierarchy"]
        )
        self.change_detector = ChangeDetector(repo_path=CONFIG["repo_path"])
        self.chat_engine = ChatEngine(CONFIG=CONFIG)

        if not os.path.exists(
            os.path.join(CONFIG["repo_path"], CONFIG["project_hierarchy"])
        ):  # 如果不存在全局结构信息文件夹.project_hierarchy，就新建一个
            self.meta_info = MetaInfo.init_from_project_path(CONFIG["repo_path"]) # 从repo_path中初始化一个meta_info, metainfo代表了整个项目的结构信息
            self.meta_info.checkpoint( # 将初始化的meta_info写入到.project_doc_record文件夹中
                target_dir_path=os.path.join(
                    CONFIG["repo_path"], CONFIG["project_hierarchy"]
                )
            )
        else: # 如果存在全局结构信息文件夹.project_hierarchy，就从中加载
            self.meta_info = MetaInfo.from_checkpoint_path(
                os.path.join(CONFIG["repo_path"], CONFIG["project_hierarchy"])
            )

        self.meta_info.white_list = load_whitelist()
        self.meta_info.checkpoint(  # 更新白名单后也要重新将全局信息写入到.project_doc_record文件夹中
            target_dir_path=os.path.join(
                CONFIG["repo_path"], CONFIG["project_hierarchy"]
            )
        )
        self.runner_lock = threading.Lock()

    def get_all_pys(self, directory):
        """
        Get all Python files in the given directory.

        Args:
            directory (str): The directory to search.

        Returns:
            list: A list of paths to all Python files.
        """
        python_files = []

        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    python_files.append(os.path.join(root, file))

        return python_files

    def generate_doc_for_a_single_item(self, doc_item: DocItem):
        """为一个对象生成文档"""
        try:
            rel_file_path = doc_item.get_full_name()

            ignore_list = CONFIG.get("ignore_list", [])
            if not need_to_generate(doc_item, ignore_list):
                logger.info(f"内容被忽略/文档已生成，跳过：{doc_item.get_full_name()}")
            else:
                logger.info(f" -- 正在生成{doc_item.get_full_name()} 对象文档...")
                file_handler = FileHandler(CONFIG["repo_path"], rel_file_path)
                response_message = self.chat_engine.generate_doc(
                    doc_item=doc_item,
                    file_handler=file_handler,
                )
                doc_item.md_content.append(response_message.content)
                doc_item.item_status = DocItemStatus.doc_up_to_date
                self.meta_info.checkpoint(
                    target_dir_path=os.path.join(
                        CONFIG["repo_path"], CONFIG["project_hierarchy"]
                    )
                )
        except Exception as e:
            logger.info(f" 多次尝试后生成文档失败，跳过：{doc_item.get_full_name()}")
            logger.info("Error:", e)
            doc_item.item_status = DocItemStatus.doc_has_not_been_generated

    def first_generate(self):
        """
        生成所有文档,
        如果生成结束，self.meta_info.document_version会变成0(之前是-1)
        每生成一个obj的doc，会实时同步回文件系统里。如果中间报错了，下次会自动load，按照文件顺序接着生成。
        **注意**：这个生成first_generate的过程中，目标仓库代码不能修改。也就是说，一个document的生成过程必须绑定代码为一个版本。
        """
        logger.info("Starting to generate documentation")
        ignore_list = CONFIG.get("ignore_list", [])
        check_task_available_func = partial(need_to_generate, ignore_list=ignore_list)
        task_manager = self.meta_info.get_topology(
            check_task_available_func
        )  # 将按照此顺序生成文档
        # topology_list = [item for item in topology_list if need_to_generate(item, ignore_list)]
        before_task_len = len(task_manager.task_dict)

        if not self.meta_info.in_generation_process:
            self.meta_info.in_generation_process = True
            logger.info("Init a new task-list")
        else:
            logger.info("Load from an existing task-list")
        self.meta_info.print_task_list(task_manager.task_dict)      

        try:
            task_manager.sync_func = self.markdown_refresh
            threads = [
                threading.Thread(
                    target=worker,
                    args=(
                        task_manager,
                        process_id,
                        self.generate_doc_for_a_single_item,
                    ),
                )
                for process_id in range(CONFIG["max_thread_count"])
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.meta_info.document_version = (
                self.change_detector.repo.head.commit.hexsha
            )
            self.meta_info.in_generation_process = False
            self.meta_info.checkpoint(
                target_dir_path=os.path.join(
                    CONFIG["repo_path"], CONFIG["project_hierarchy"]
                )
            )
            logger.info(
                f"成功生成了 {before_task_len - len(task_manager.task_dict)} 个文档"
            )

        except BaseException as e:
            logger.info(
                f"Finding an error as {e}, {before_task_len - len(task_manager.task_dict)} docs are generated at this time"
            )

    def markdown_refresh(self):
        """将目前最新的document信息写入到一个markdown格式的文件夹里(不管markdown内容是不是变化了)"""
        with self.runner_lock:
            # 首先删除doc下所有内容，然后再重新写入 (这种方法有点问题吧？@yeyn)
            markdown_docs_path = os.path.join(CONFIG["repo_path"], CONFIG["Markdown_Docs_folder"])
            if os.path.exists(markdown_docs_path):
                shutil.rmtree(markdown_docs_path)
            os.mkdir(markdown_docs_path)

            file_item_list = self.meta_info.get_all_files()
            for file_item in tqdm(file_item_list):

                def recursive_check(
                    doc_item: DocItem,
                ) -> bool:  # 检查一个file内是否存在doc
                    if doc_item.md_content != []:
                        return True
                    for _, child in doc_item.children.items():
                        if recursive_check(child):
                            return True
                    return False

                if recursive_check(file_item) == False:
                    # logger.info(f"不存在文档内容，跳过：{file_item.get_full_name()}")
                    continue
                rel_file_path = file_item.get_full_name()

                # file_handler = FileHandler(CONFIG['repo_path'], rel_file_path)
                def to_markdown(item: DocItem, now_level: int) -> str:
                    markdown_content = ""
                    markdown_content += (
                        "#" * now_level + f" {item.item_type.to_str()} {item.obj_name}"
                    )
                    if (
                        "params" in item.content.keys()
                        and len(item.content["params"]) > 0
                    ):
                        markdown_content += f"({', '.join(item.content['params'])})"
                    markdown_content += "\n"
                    markdown_content += f"{item.md_content[-1] if len(item.md_content) >0 else 'Doc is waiting to be generated...'}\n"
                    for _, child in item.children.items():
                        markdown_content += to_markdown(child, now_level + 1)
                        markdown_content += "***\n"

                    return markdown_content

                markdown = ""
                for _, child in file_item.children.items():
                    markdown += to_markdown(child, 2)
                assert markdown != None, f"markdown内容为空，文件路径为{rel_file_path}"
                # 写入markdown内容到.md文件
                file_path = os.path.join(
                    CONFIG["Markdown_Docs_folder"],
                    file_item.get_file_name().replace(".py", ".md"),
                )
                if file_path.startswith("/"):
                    # 移除开头的 '/'
                    file_path = file_path[1:]
                abs_file_path = os.path.join(CONFIG["repo_path"], file_path)
                os.makedirs(os.path.dirname(abs_file_path), exist_ok=True)
                with open(abs_file_path, "w", encoding="utf-8") as file:
                    file.write(markdown)

            logger.info(
                f"markdown document has been refreshed at {CONFIG['Markdown_Docs_folder']}"
            )

    def git_commit(self, commit_message):
        try:
            subprocess.check_call(
                ["git", "commit", "--no-verify", "-m", commit_message]
            )
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while trying to commit {str(e)}")

    def run(self):
        """
        Runs the document update process.

        This method detects the changed Python files, processes each file, and updates the documents accordingly.

        Returns:
            None
        """

        if self.meta_info.document_version == "":
            # 根据document version自动检测是否仍在最初生成的process里(是否为第一次生成)
            self.first_generate() # 如果是第一次做文档生成任务，就通过first_generate生成所有文档
            self.meta_info.checkpoint(
                target_dir_path=os.path.join(
                    CONFIG["repo_path"], CONFIG["project_hierarchy"]
                ),
                flash_reference_relation=True,
            ) # 这一步将生成后的meta信息（包含引用关系）写入到.project_doc_record文件夹中
            return

        if not self.meta_info.in_generation_process: # 如果不是在生成过程中，就开始检测变更
            logger.info("Starting to detect changes.")

            """采用新的办法
            1.新建一个project-hierachy
            2.和老的hierarchy做merge,处理以下情况：
            - 创建一个新文件：需要生成对应的doc
            - 文件、对象被删除：对应的doc也删除(按照目前的实现，文件重命名算是删除再添加)
            - 引用关系变了：对应的obj-doc需要重新生成
            
            merge后的new_meta_info中：
            1.新建的文件没有文档，因此metainfo merge后还是没有文档
            2.被删除的文件和obj，本来就不在新的meta里面，相当于文档被自动删除了
            3.只需要观察被修改的文件，以及引用关系需要被通知的文件去重新生成文档"""
            new_meta_info = MetaInfo.init_from_project_path(CONFIG["repo_path"]) # 从repo_path中初始化一个meta_info, metainfo代表了整个项目的结构信息
            new_meta_info.load_doc_from_older_meta(self.meta_info) # 从老的meta_info中加载文档信息, 目的是跟上面的new_meta_info做merge，检测出new中的变更

            self.meta_info = new_meta_info # 更新自身的meta_info信息为new的信息
            self.meta_info.in_generation_process = True # 将in_generation_process设置为True，表示检测到变更后正在生成文档的过程中

        # 处理任务队列
        ignore_list = CONFIG.get("ignore_list", [])
        check_task_available_func = partial(need_to_generate, ignore_list=ignore_list)

        task_manager = self.meta_info.get_task_manager(self.meta_info.target_repo_hierarchical_tree,task_available_func=check_task_available_func)
        
        if task_manager.all_success:
            logger.info("No tasks in the queue, all documents are completed and up to date.")
        else:
            self.meta_info.print_task_list(task_manager.task_dict)

        task_manager.sync_func = self.markdown_refresh
        threads = [
            threading.Thread(
                target=worker,
                args=(task_manager, process_id, self.generate_doc_for_a_single_item),
            )
            for process_id in range(CONFIG["max_thread_count"])
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.meta_info.in_generation_process = False
        self.meta_info.document_version = self.change_detector.repo.head.commit.hexsha

        self.meta_info.checkpoint(
            target_dir_path=os.path.join(
                CONFIG["repo_path"], CONFIG["project_hierarchy"]
            ),
            flash_reference_relation=True,
        )
        logger.info(f"Doc has been forwarded to the latest version")

        self.markdown_refresh()

    def add_new_item(self, file_handler, json_data):
        """
        Add new projects to the JSON file and generate corresponding documentation.

        Args:
            file_handler (FileHandler): The file handler object for reading and writing files.
            json_data (dict): The JSON data storing the project structure information.

        Returns:
            None
        """
        file_dict = {}
        # 因为是新增的项目，所以这个文件里的所有对象都要写一个文档
        for (
            structure_type,
            name,
            start_line,
            end_line,
            parent,
            params,
        ) in file_handler.get_functions_and_classes(file_handler.read_file()):
            code_info = file_handler.get_obj_code_info(
                structure_type, name, start_line, end_line, parent, params
            )
            response_message = self.chat_engine.generate_doc(code_info, file_handler)
            md_content = response_message.content
            code_info["md_content"] = md_content
            # 文件对象file_dict中添加一个新的对象
            file_dict[name] = code_info

        json_data[file_handler.file_path] = file_dict
        # 将新的项写入json文件
        with open(self.project_manager.project_hierarchy, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=4, ensure_ascii=False)
        logger.info(f"已将新增文件 {file_handler.file_path} 的结构信息写入json文件。")
        # 将变更部分的json文件内容转换成markdown内容
        markdown = file_handler.convert_to_markdown_file(
            file_path=file_handler.file_path
        )
        # 将markdown内容写入.md文件
        file_handler.write_file(
            os.path.join(
                self.project_manager.repo_path,
                CONFIG["Markdown_Docs_folder"],
                file_handler.file_path.replace(".py", ".md"),
            ),
            markdown,
        )
        logger.info(f"已生成新增文件 {file_handler.file_path} 的Markdown文档。")

    def process_file_changes(self, repo_path, file_path, is_new_file):
        """
        This function is called in the loop of detected changed files. Its purpose is to process changed files according to the absolute file path, including new files and existing files.
        Among them, changes_in_pyfile is a dictionary that contains information about the changed structures. An example format is: {'added': {'add_context_stack', '__init__'}, 'removed': set()}

        Args:
            repo_path (str): The path to the repository.
            file_path (str): The relative path to the file.
            is_new_file (bool): Indicates whether the file is new or not.

        Returns:
            None
        """
        file_handler = FileHandler(
            repo_path=repo_path, file_path=file_path
        )  # 变更文件的操作器
        # 获取整个py文件的代码
        source_code = file_handler.read_file()
        changed_lines = self.change_detector.parse_diffs(
            self.change_detector.get_file_diff(file_path, is_new_file)
        )
        changes_in_pyfile = self.change_detector.identify_changes_in_structure(
            changed_lines, file_handler.get_functions_and_classes(source_code)
        )
        logger.info(f"检测到变更对象：\n{changes_in_pyfile}")

        # 判断project_hierarchy.json文件中能否找到对应.py文件路径的项
        with open(self.project_manager.project_hierarchy, "r", encoding="utf-8") as f:
            json_data = json.load(f)

        # 如果找到了对应文件
        if file_handler.file_path in json_data:
            # 更新json文件中的内容
            json_data[file_handler.file_path] = self.update_existing_item(
                json_data[file_handler.file_path], file_handler, changes_in_pyfile
            )
            # 将更新后的file写回到json文件中
            with open(
                self.project_manager.project_hierarchy, "w", encoding="utf-8"
            ) as f:
                json.dump(json_data, f, indent=4, ensure_ascii=False)

            logger.info(f"已更新{file_handler.file_path}文件的json结构信息。")

            # 将变更部分的json文件内容转换成markdown内容
            markdown = file_handler.convert_to_markdown_file(
                file_path=file_handler.file_path
            )
            # 将markdown内容写入.md文件
            file_handler.write_file(
                os.path.join(
                    CONFIG["Markdown_Docs_folder"],
                    file_handler.file_path.replace(".py", ".md"),
                ),
                markdown,
            )
            logger.info(f"已更新{file_handler.file_path}文件的Markdown文档。")

        # 如果没有找到对应的文件，就添加一个新的项
        else:
            self.add_new_item(file_handler, json_data)

        # 将run过程中更新的Markdown文件（未暂存）添加到暂存区
        git_add_result = self.change_detector.add_unstaged_files()

        if len(git_add_result) > 0:
            logger.info(f"已添加 {[file for file in git_add_result]} 到暂存区")

        # self.git_commit(f"Update documentation for {file_handler.file_path}") # 提交变更

    def update_existing_item(self, file_dict, file_handler, changes_in_pyfile):
        """
        Update existing projects.

        Args:
            file_dict (dict): A dictionary containing file structure information.
            file_handler (FileHandler): The file handler object.
            changes_in_pyfile (dict): A dictionary containing information about the objects that have changed in the file.

        Returns:
            dict: The updated file structure information dictionary.
        """
        new_obj, del_obj = self.get_new_objects(file_handler)

        # 处理被删除的对象
        for obj_name in del_obj:  # 真正被删除的对象
            if obj_name in file_dict:
                del file_dict[obj_name]
                logger.info(f"已删除 {obj_name} 对象。")

        referencer_list = []

        # 生成文件的结构信息，获得当前文件中的所有对象， 这里其实就是文件更新之后的结构了
        current_objects = file_handler.generate_file_structure(file_handler.file_path)

        current_info_dict = {obj["name"]: obj for obj in current_objects.values()}

        # 更新全局文件结构信息，比如代码起始行\终止行等
        for current_obj_name, current_obj_info in current_info_dict.items():
            if current_obj_name in file_dict:
                # 如果当前对象在旧对象列表中存在，更新旧对象的信息
                file_dict[current_obj_name]["type"] = current_obj_info["type"]
                file_dict[current_obj_name]["code_start_line"] = current_obj_info[
                    "code_start_line"
                ]
                file_dict[current_obj_name]["code_end_line"] = current_obj_info[
                    "code_end_line"
                ]
                file_dict[current_obj_name]["parent"] = current_obj_info["parent"]
                file_dict[current_obj_name]["name_column"] = current_obj_info[
                    "name_column"
                ]
            else:
                # 如果当前对象在旧对象列表中不存在，将新对象添加到旧对象列表中
                file_dict[current_obj_name] = current_obj_info

        # 对于每一个对象：获取其引用者列表
        for obj_name, _ in changes_in_pyfile["added"]:
            for current_object in current_objects.values():  # 引入new_objects的目的是获取到find_all_referencer中必要的参数信息。在changes_in_pyfile['added']中只有对象和其父级结构的名称，缺少其他参数
                if (
                    obj_name == current_object["name"]
                ):  # 确保只有当added中的对象名称匹配new_objects时才添加引用者
                    # 获取每个需要生成文档的对象的引用者
                    referencer_obj = {
                        "obj_name": obj_name,
                        "obj_referencer_list": self.project_manager.find_all_referencer(
                            variable_name=current_object["name"],
                            file_path=file_handler.file_path,
                            line_number=current_object["code_start_line"],
                            column_number=current_object["name_column"],
                        ),
                    }
                    referencer_list.append(
                        referencer_obj
                    )  # 对于每一个正在处理的对象，添加他的引用者字典到全部对象的应用者列表中

        with ThreadPoolExecutor(max_workers=5) as executor:
            # 通过线程池并发执行
            futures = []
            for changed_obj in changes_in_pyfile["added"]:  # 对于每一个待处理的对象
                for ref_obj in referencer_list:
                    if (
                        changed_obj[0] == ref_obj["obj_name"]
                    ):  # 在referencer_list中找到它的引用者字典！
                        future = executor.submit(
                            self.update_object,
                            file_dict,
                            file_handler,
                            changed_obj[0],
                            ref_obj["obj_referencer_list"],
                        )
                        logger.info(
                            f"正在生成 {file_handler.file_path}中的{changed_obj[0]} 对象文档..."
                        )
                        futures.append(future)

            for future in futures:
                future.result()

        # 更新传入的file参数
        return file_dict

    def update_object(self, file_dict, file_handler, obj_name, obj_referencer_list):
        """
        Generate documentation content and update corresponding field information of the object.

        Args:
            file_dict (dict): A dictionary containing old object information.
            file_handler: The file handler.
            obj_name (str): The object name.
            obj_referencer_list (list): The list of object referencers.

        Returns:
            None
        """
        if obj_name in file_dict:
            obj = file_dict[obj_name]
            response_message = self.chat_engine.generate_doc(
                obj, file_handler, obj_referencer_list
            )
            obj["md_content"] = response_message.content

    def get_new_objects(self, file_handler):
        """
        The function gets the added and deleted objects by comparing the current version and the previous version of the .py file.

        Args:
            file_handler (FileHandler): The file handler object.

        Returns:
            tuple: A tuple containing the added and deleted objects, in the format (new_obj, del_obj)

        Output example:
            new_obj: ['add_context_stack', '__init__']
            del_obj: []
        """
        current_version, previous_version = file_handler.get_modified_file_versions()
        parse_current_py = file_handler.get_functions_and_classes(current_version)
        parse_previous_py = (
            file_handler.get_functions_and_classes(previous_version)
            if previous_version
            else []
        )

        current_obj = {f[1] for f in parse_current_py}
        previous_obj = {f[1] for f in parse_previous_py}

        new_obj = list(current_obj - previous_obj)
        del_obj = list(previous_obj - current_obj)
        return new_obj, del_obj


if __name__ == "__main__":
    runner = Runner()

    # runner.meta_info.target_repo_hierarchical_tree.print_recursive()
    runner.run()

    logger.info("文档任务完成。")
