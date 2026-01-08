import requests
from bs4 import BeautifulSoup
import os
import threading
import pandas as pd
import re
from queue import Queue
import time
import sys
import glob
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# ================= 配置区域 =================

# 深度伪装浏览器请求头
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Connection': 'keep-alive'
}

# 忽略 SSL 证书警告
requests.packages.urllib3.disable_warnings()

# 定义可能的列名变体（不区分大小写）
DOI_COL_VARIANTS = ['doi', 'di', 'article_doi', 'doi_link', 'accession_number']
TITLE_COL_VARIANTS = ['title', 'article_title', 'ti', 'publication_title', 'article title']

# ================= 辅助函数 =================

def clean_filename(text, max_len=80):
    """清理文件名，移除非法字符"""
    if not text:
        return "Unknown_Title"
    cleaned = re.sub(r'[\\/:*?"<>|]', '_', str(text))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len]

def load_domains(file_path='domains.txt'):
    """从外部文件加载域名，并自动补全 https://"""
    domains = []
    if not os.path.exists(file_path):
        print(f"[警告] 找不到 {file_path}，将使用内置默认域名。")
        return ["https://sci-hub.se/  ", "https://sci-hub.st/  ", "https://sci-hub.ru/  "]
    
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                # === 核心修复：自动添加 https:// 前缀 ===
                if not line.startswith("http://") and not line.startswith("https://"):
                    line = "https://" + line
                
                if not line.endswith('/'):
                    line += '/'
                domains.append(line)
    
    if not domains:
        print("[警告] 域名文件为空，使用默认域名。")
        return ["https://sci-hub.se/  "]
    
    print(f"[系统] 已加载 {len(domains)} 个可用域名: {domains}")
    return domains

def find_column_name(df, variants):
    """在DataFrame中模糊查找匹配的列名"""
    columns = df.columns
    for col in columns:
        if str(col).lower().strip() in variants:
            return col
    for col in columns:
        for v in variants:
            if v in str(col).lower():
                return col
    return None

def read_all_excel_files(folder_path):
    """读取文件夹下所有Excel文件并合并"""
    all_files = glob.glob(os.path.join(folder_path, "*.xls*"))
    combined_tasks = []
    
    print(f"[系统] 正在扫描 '{folder_path}' 下的 Excel 文件...")
    if not all_files:
        print(f"[错误] '{folder_path}' 文件夹是空的或不存在Excel文件。")
        return []

    for file in all_files:
        try:
            df = pd.read_excel(file)
            doi_col = find_column_name(df, DOI_COL_VARIANTS)
            title_col = find_column_name(df, TITLE_COL_VARIANTS)
            
            if not doi_col:
                print(f"[跳过] 文件 '{os.path.basename(file)}' 未找到 DOI 列。")
                continue
            
            print(f"[读取] 文件 '{os.path.basename(file)}' (DOI列: {doi_col}, 标题列: {title_col})")
            
            for index, row in df.iterrows():
                doi = str(row[doi_col]).strip()
                if not doi or len(doi) < 5 or doi.lower() == 'nan':
                    continue
                
                title = "Unknown_Title"
                if title_col and pd.notna(row[title_col]):
                    title = str(row[title_col]).strip()
                
                combined_tasks.append((doi, title))
                
        except Exception as e:
            print(f"[错误] 读取文件 {file} 失败: {e}")
            
    unique_tasks = list(set(combined_tasks))
    print(f"[统计] 共提取到 {len(combined_tasks)} 条数据，去重后剩余 {len(unique_tasks)} 个下载任务。")
    return unique_tasks

# ================= 核心线程逻辑 =================

def download_worker(queue, domains, save_dir, logs, processing_list, stats, lock):
    """下载工作线程"""
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))
    session.mount('http://', HTTPAdapter(max_retries=retries))

    while not queue.empty():
        try:
            doi, title = queue.get(block=False)
        except:
            break

        safe_title = clean_filename(title)
        safe_doi = clean_filename(doi, 30)
        task_id = f"{safe_doi}"
        
        with lock:
            task_info = {'id': task_id, 'title': safe_title[:20], 'status': 'Connecting...'}
            processing_list.append(task_info)
        
        success = False
        target_path = os.path.join(save_dir, f"{safe_title}.pdf")
        
        if safe_title == "Unknown_Title":
            target_path = os.path.join(save_dir, f"{clean_filename(doi)}.pdf")

        if os.path.exists(target_path):
             with lock:
                stats['exists'] += 1
                stats['completed'] += 1
                logs['success'].append(f"{doi}\t已存在跳过")
                queue.task_done()
                if task_info in processing_list: processing_list.remove(task_info)
             continue

        last_error = ""

        for domain in domains:
            try:
                base_url = domain.rstrip("/")
                url = f"{base_url}/{doi}"
                
                # 1. 获取详情页
                r = session.get(url, timeout=20, verify=False, allow_redirects=True)
                r.raise_for_status()
                
                if "captcha" in r.text.lower():
                    raise Exception("被Sci-Hub拦截(验证码)")

                soup = BeautifulSoup(r.text, "html.parser")
                
                # 2. 解析PDF链接
                download_url = None
                if soup.iframe and soup.iframe.get("src"):
                    download_url = soup.iframe.get("src")
                elif soup.embed and soup.embed.get("src"):
                    download_url = soup.embed.get("src")
                elif soup.find("div", {"id": "buttons"}):
                     btn = soup.find("div", {"id": "buttons"}).find("a")
                     if btn: download_url = btn.get("onclick").split("'")[1]

                if not download_url:
                    if r.url.endswith(".pdf"):
                        download_url = r.url
                    else:
                        raise Exception("页面未找到PDF链接")
                
                # 补全 URL
                if download_url.startswith("//"):
                    download_url = "https:" + download_url
                elif download_url.startswith("/"):
                    download_url = base_url + download_url
                elif not download_url.startswith("http"):
                    # 处理罕见的相对路径且没有斜杠的情况
                    download_url = base_url + "/" + download_url

                with lock:
                     for item in processing_list:
                        if item['id'] == task_id: item['status'] = 'Downloading PDF...'

                # 3. 下载文件
                pdf_r = session.get(download_url, timeout=30, verify=False)
                
                if len(pdf_r.content) < 1000 or b"%PDF-" not in pdf_r.content[:20]:
                     raise Exception("下载内容不是有效的PDF文件")

                with open(target_path, "wb") as f:
                    f.write(pdf_r.content)
                success = True
                break 
                    
            except Exception as e:
                last_error = str(e)[:50]
                continue

        with lock:
            if success:
                stats['success'] += 1
                logs['success'].append(f"{doi}\t下载成功")
            else:
                stats['failed'] += 1
                logs['error'].append(f"{doi}\t{safe_title}\t失败原因: {last_error}")
            
            stats['completed'] += 1
            if task_info in processing_list:
                processing_list.remove(task_info)
        
        queue.task_done()

# ================= 进度显示线程 =================

def progress_display(total, processing_list, stats, lock, stop_event):
    """控制台进度刷新"""
    start_time = time.time()
    
    while not stop_event.is_set():
        elapsed = time.time() - start_time
        with lock:
            curr_completed = stats['completed']
            curr_success = stats['success']
            curr_failed = stats['failed']
            curr_exists = stats['exists']
            current_tasks = list(processing_list)
        
        speed = curr_completed / elapsed if elapsed > 0 else 0
        percent = (curr_completed / total) * 100 if total > 0 else 0
        
        os.system('cls' if os.name == 'nt' else 'clear')
        
        print("="*80)
        print(f" Sci-Hub 批量下载器 v3.1 (URL自动修复版) - 正在运行")
        print("="*80)
        print(f" [总体进度]: {percent:5.1f}% | 已完成: {curr_completed}/{total}")
        print(f" [详细统计]: ✅ 成功: {curr_success} | ❌ 失败: {curr_failed} | 📂 跳过: {curr_exists}")
        print(f" [平均速度]: {speed:.2f} 篇/秒 | 耗时: {elapsed:.1f} 秒")
        print("-" * 80)
        print(" [当前线程状态]:")
        
        for idx, task in enumerate(current_tasks[:8]):
            print(f"   {idx+1}. [{task['status']}] {task['title']}...")
            
        print("-" * 80)
        
        if curr_completed >= total:
            break
            
        time.sleep(0.5)

# ================= 主程序入口 =================

if __name__ == "__main__":
    INPUT_FOLDER = r"./excel_files"
    DOWNLOAD_FOLDER = r"./downloaded_pdfs"
    DOMAIN_FILE = "domains.txt"
    THREAD_COUNT = 5

    if not os.path.exists(INPUT_FOLDER):
        os.makedirs(INPUT_FOLDER)
        print(f"[提示] 已创建 '{INPUT_FOLDER}'，请将Excel文件放入其中后重新运行。")
        sys.exit()
    if not os.path.exists(DOWNLOAD_FOLDER):
        os.makedirs(DOWNLOAD_FOLDER)

    sci_hub_domains = load_domains(DOMAIN_FILE)
    tasks = read_all_excel_files(INPUT_FOLDER)

    if not tasks:
        print("没有找到任何任务，程序退出。")
        sys.exit()

    task_queue = Queue()
    for t in tasks:
        task_queue.put(t)

    total_tasks = len(tasks)
    lock = threading.Lock()
    
    stats = {'completed': 0, 'success': 0, 'failed': 0, 'exists': 0}
    logs = {'success': [], 'error': []}
    processing_list = []
    
    stop_event = threading.Event()

    ui_thread = threading.Thread(target=progress_display, args=(total_tasks, processing_list, stats, lock, stop_event))
    ui_thread.daemon = True
    ui_thread.start()

    workers = []
    for _ in range(THREAD_COUNT):
        t = threading.Thread(target=download_worker, args=(task_queue, sci_hub_domains, DOWNLOAD_FOLDER, logs, processing_list, stats, lock))
        t.daemon = True
        t.start()
        workers.append(t)

    task_queue.join()
    stop_event.set()
    time.sleep(1)

    with open("download_success.log", "w", encoding="utf-8") as f:
        f.writelines([line + "\n" for line in logs['success']])
    
    with open("download_error.log", "w", encoding="utf-8") as f:
        f.writelines([line + "\n" for line in logs['error']])

    print("\n\n所有任务处理完毕！")