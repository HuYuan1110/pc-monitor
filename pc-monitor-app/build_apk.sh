#!/usr/bin/env bash
# ============================================================
# 电脑监测助手 APK 打包脚本（带构建校验）
# 用法：bash build_apk.sh
# 步骤：校验源 → cap sync → 校验资产 → gradle 构建 → 复制重命名
# 目的：杜绝「改了前端却没打进去」——构建前/后都校验版本标记。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

# 版本标记（必须与 www/index.html 顶部的可见标记完全一致）
UI_MARK="UI build 3.1.7"
APK_NAME="电脑监测_v3.1.7.apk"

# JAVA_HOME 可覆盖；默认 jbr-21（本项目固定构建环境）
if [ -z "${JAVA_HOME:-}" ]; then
  export JAVA_HOME="C:/Users/22894/.jdks/jbr-21.0.11"
fi
export PATH="$JAVA_HOME/bin:$PATH"

SRC="www/index.html"
ASSET="android/app/src/main/assets/public/index.html"

echo "============================================================"
echo "  电脑监测助手 APK 打包（校验构建）"
echo "  版本标记：$UI_MARK"
echo "============================================================"

# 1) 构建前校验源文件
if grep -qF "$UI_MARK" "$SRC"; then
  echo "✅ 源文件 $SRC 含标记 \"$UI_MARK\""
else
  echo "❌ 构建中止：$SRC 缺少标记 \"$UI_MARK\"（源码未更新到最新版本？）"
  exit 1
fi

# 2) 同步 www → android 工程
echo "→ 同步 www 到 android 工程（cap sync）..."
npx cap sync android

# 3) 构建后校验打包资产（关键：杜绝改了没打进去）
if grep -qF "$UI_MARK" "$ASSET"; then
  echo "✅ 打包资产 $ASSET 含标记 \"$UI_MARK\""
else
  echo "❌ 构建中止：$ASSET 缺少标记 \"$UI_MARK\"（cap sync 未把最新源同步进去？）"
  exit 1
fi

# 4) Gradle 构建 debug APK
echo "→ Gradle 构建 debug APK..."
( cd android && ./gradlew assembleDebug )

# 5) 复制并重命名
APK_SRC="android/app/build/outputs/apk/debug/app-debug.apk"
if [ ! -f "$APK_SRC" ]; then
  echo "❌ 构建中止：未找到 APK 产物 $APK_SRC"
  exit 1
fi
cp "$APK_SRC" "$APK_NAME"
echo "✅ 生成 $APK_NAME"
ls -la "$APK_NAME"
echo "============================================================"
echo "  打包完成，可安装 $APK_NAME"
echo "============================================================"
