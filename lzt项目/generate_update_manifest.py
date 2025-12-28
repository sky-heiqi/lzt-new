#!/usr/bin/env python3
"""
更新清单生成工具

此脚本用于生成更新清单，包含所有可更新文件的MD5哈希值和大小信息。
生成的清单可以用于配置 update_manifest.php

使用方法：
    python generate_update_manifest.py

输出：
    - 在控制台打印PHP数组格式的文件清单
    - 生成 update_files.json 文件
"""

import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime


# 可更新的文件列表（相对路径）
UPDATABLE_FILES = [
    # 前端文件（不需要重启）
    'static/version.txt',
    'static/js/app.js',
    # 后端核心文件（需要重启）
    'reply_server.py',
    'XianyuAutoAsync.py',
    'db_manager.py',
    # 'cookie_manager.py',
    # 'ai_reply_engine.py',
    # 'auto_updater.py',
    # 'config.py',
    # 'Start.py',
    
    # 工具文件
    # 'utils/xianyu_utils.py',
    # 'utils/message_utils.py',
    # 'utils/image_utils.py',
    # 'utils/qr_login.py',
    # 'utils/refresh_util.py',
    
    # 配置文件模板（不更新用户的实际配置）
    # 'global_config.yml',  # 用户配置，不更新
]

# 不需要重启的文件扩展名
NO_RESTART_EXTENSIONS = {'.js', '.css', '.html', '.json', '.yml', '.yaml'}


def calculate_md5(file_path: Path) -> str:
    """计算文件MD5"""
    if not file_path.exists():
        return ""
    
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def get_file_size(file_path: Path) -> int:
    """获取文件大小"""
    if not file_path.exists():
        return 0
    return file_path.stat().st_size


def needs_restart(file_path: str) -> bool:
    """判断文件更新是否需要重启"""
    ext = Path(file_path).suffix.lower()
    return ext not in NO_RESTART_EXTENSIONS


def generate_manifest(base_dir: Path, version: str = "v1.0.8") -> dict:
    """生成更新清单"""
    files = []
    
    for file_path in UPDATABLE_FILES:
        full_path = base_dir / file_path
        
        if not full_path.exists():
            print(f"警告: 文件不存在 - {file_path}")
            continue
        
        md5 = calculate_md5(full_path)
        size = get_file_size(full_path)
        
        files.append({
            'path': file_path.replace('\\', '/'),
            'md5': md5,
            'size': size,
            'requires_restart': needs_restart(file_path),
            'description': '',
        })
    
    manifest = {
        'version': version,
        'release_date': datetime.now().strftime('%Y-%m-%d'),
        'description': f'版本 {version} 更新',
        'min_version': 'v1.0.0',
        'changelog': [
            '热更新功能',
        ],
        'files': files,
    }
    
    return manifest


def print_php_array(manifest: dict, base_url: str = "http://116.196.116.76/xianyu/xianyu-update-files"):
    """打印PHP数组格式"""
    print("\n" + "=" * 60)
    print("PHP 文件清单配置（复制到 update_manifest.php）")
    print("=" * 60 + "\n")
    
    print("$fileManifest = [")
    for file in manifest['files']:
        restart_str = 'true' if file['requires_restart'] else 'false'
        print(f"    [")
        print(f"        'path' => '{file['path']}',")
        print(f"        'md5' => '{file['md5']}',")
        print(f"        'size' => {file['size']},")
        print(f"        'requires_restart' => {restart_str},")
        print(f"        'description' => '{file['description']}',")
        print(f"    ],")
    print("];")
    
    print("\n" + "=" * 60)
    print("版本信息")
    print("=" * 60)
    print(f"版本号: {manifest['version']}")
    print(f"发布日期: {manifest['release_date']}")
    print(f"文件数量: {len(manifest['files'])}")
    total_size = sum(f['size'] for f in manifest['files'])
    print(f"总大小: {total_size / 1024:.2f} KB")


def main():
    # 获取项目根目录
    if len(sys.argv) > 1:
        base_dir = Path(sys.argv[1])
    else:
        base_dir = Path(__file__).parent
    
    # 获取版本号
    version = "v1.0.8"
    if len(sys.argv) > 2:
        version = sys.argv[2]
    
    print(f"项目目录: {base_dir}")
    print(f"版本号: {version}")
    
    # 生成清单
    manifest = generate_manifest(base_dir, version)
    
    # 保存JSON文件
    output_file = base_dir / "update_files.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n已生成: {output_file}")
    
    # 打印PHP格式
    print_php_array(manifest)
    
    print("\n" + "=" * 60)
    print("使用说明")
    print("=" * 60)
    print("""
1. 将上面的 $fileManifest 数组复制到服务器上的 update_manifest.php 文件中

2. 将需要更新的文件上传到服务器的对应目录：
   http://116.196.116.76/xianyu-update-files/
   
   目录结构应该是：
   xianyu-update-files/
   ├── static/
   │   ├── js/
   │   │   └── app.js
   │   ├── css/
   │   │   └── app.css
   │   └── index.html
   ├── reply_server.py
   ├── XianyuAutoAsync.py
   └── ...

3. 更新 update_manifest.php 中的版本号和更新日志

4. 用户在前端点击"一键热更新"即可自动下载更新
""")


if __name__ == '__main__':
    main()

