# DCS Head Tracker(webcam)

webcam → MediaPipe 頭部姿態 → 濾波/曲線 → UDP → **opentrack** → DCS World
DCS 原生支援 FreeTrack/TrackIR,所以 DCS 裡不用改任何設定。

## 換電腦使用

```
git clone <這個 repo 的網址>
cd dcs-head-tracker
INSTALL.bat
```

需要 Windows + Python 3.10~3.12(安裝時勾選 Add python.exe to PATH)+ [opentrack](https://github.com/opentrack/opentrack/releases)(裝好即可)。沒裝 git 的話,在 GitHub 頁面按 Code → Download ZIP,解壓後雙擊 `INSTALL.bat`。設定檔 `~/.dcs_headtracker.json` 是各台電腦自己的,不會被帶走。

## 最簡單的用法(裝一次,之後什麼都不用按)

1. 安裝 [opentrack](https://github.com/opentrack/opentrack/releases)(只要裝,不用開、不用設定)。
2. 雙擊 **INSTALL.bat**(自動找 Python、安裝依賴、建立桌面捷徑、啟動程式)。
3. 完成。第一次啟動時程式會自動:
   - 設定好 opentrack(輸入 UDP、輸出 freetrack、偵測到 `DCS.exe` 時自動開始追蹤)並幫你把它開在系統匣;
   - 開啟「隨 Windows 啟動(縮在系統匣)」。

之後的日常:**什麼都不用做**。開機後程式就在系統匣待命,你一開 DCS,它會自動開鏡頭、開始追蹤;關 DCS,鏡頭自動關掉。想手動用也可以:雙擊桌面的 **DCS Head Tracker**,按大大的 **Start tracking**。再次雙擊捷徑只會把已經在跑的視窗叫出來,不會重複開。

進 DCS 後看著螢幕正中央按 **Alt+C**(會「嗶」一聲)重置中心。

## 首頁怎麼看

- 上方大卡片用一句話告訴你現在的狀態:綠色 = 正常追蹤;黃色 = 正在處理/需要你注意;紅色 = 有問題;卡片上有需要時才出現的按鈕(例如 **Install opentrack**、**Restart opentrack**、**Try again**)。
- 四顆小標籤:Camera(鏡頭有看到臉)、opentrack(有在執行)、Output(opentrack 真的有把資料交出去)、DCS(遊戲有在跑)。
- **Start tracking / Stop**、**Recenter**、鏡頭預覽、Yaw/Pitch/Roll 三條。
- 右上角 **⚙ Settings** 才是進階設定(手感滑桿、鏡頭選擇、熱鍵、自動化開關、Connection check)。

## 自動化(Settings → Automation)

- **Start tracking automatically when DCS.exe starts**:每 2 秒看一次有沒有 `DCS.exe`,出現就自動追蹤;DCS 關閉時自動停止並放開鏡頭。你手動按過 Stop,這一次 DCS 執行期間不會再自動重開。
- **Set up and start opentrack for me**:程式啟動、DCS 啟動、或你剛裝好 opentrack 時,自動寫入設定並啟動 opentrack。設定檔放在 `文件\opentrack-2.3\DCS-HeadTracker.ini`;如果你原本有同名但不同內容的檔案,會先備份成 `.bak`。不想讓程式碰 opentrack 就關掉這項。
- **Start with Windows (hidden in the tray)**:第一次啟動自動打開,可在這裡關掉。寫入目前使用者的啟動項目,不需要系統管理員。關閉視窗只會縮到系統匣,右鍵系統匣圖示 → Quit 才會真的結束。
- **Beep when recentered**、**Recenter hotkey**(預設 `alt+c`,可改成 `ctrl+shift+c`、`f10` 等;暫停鍵固定 F11)。

## 熱鍵細節

- 用 Windows 內建的 RegisterHotKey 註冊,不需要額外套件。按鍵會被本程式攔下,不會再傳給 DCS。如果提示「already used by another program」,代表別的程式(顯卡/錄影軟體)佔用了,改一個組合即可。
- 重置的是「目前追蹤中的中心」,所以追蹤要在執行中才有東西可重置。
- 在 DCS 前景時完全沒反應:試試用系統管理員身分執行本程式。

## Settings 裡的選項

- **Test sweep**:不用鏡頭,送一段左右掃描,單獨檢查 opentrack → DCS 這一段。
- **Pause**(F11:暫停,視角回正)。
- 滑桿即時生效,設定自動記住(`~/.dcs_headtracker.json`)。
  - **Yaw/Pitch gain**:轉頭到 45° 時的放大倍率,正中央固定 1.0 以保持精準。
  - **Smoothing**:越大越穩,但延遲越高。**Responsiveness**:越大,快速轉頭時延遲越低。
  - **Lean (x/y/z)**:前後左右傾身。webcam 深度雜訊較大,預設關閉。
- **Invert yaw/pitch/roll**:進 DCS 後方向反了就勾。

## 想要獨立的 exe

雙擊 **build_exe.bat**(需要 Python,第一次約幾分鐘),完成後在 `dist\DCS-HeadTracker\DCS-HeadTracker.exe`,整個資料夾可以複製到任何地方。臉部模型會一起打包。啟動就閃退時改跑 `build_exe.bat debug`,會保留一個看得到錯誤訊息的主控台。

## 出問題時

首頁卡片會直接說原因。常見情況:

- **opentrack is not installed**:按卡片上的 **Install opentrack**(桌面上有安裝檔就直接執行,沒有就開下載頁)。裝完回到程式,它會自己設定並啟動 opentrack。
- **opentrack isn't passing data yet**(DCS 已開但沒資料):多半是 opentrack 在設定寫入前就已經開著。按 **Restart opentrack**(會強制關閉 opentrack 再用新設定開啟)。
- **鏡頭燈沒亮 / Camera problem**:別的程式(Zoom、Teams、OBS)佔用鏡頭,在 Settings 用 **Scan** 換編號;或 Windows 設定 → 隱私權 → 相機 沒開給桌面應用程式。
- **Can't see your face**:正對光源、避免背光;解析度改 1280×720 通常更穩。轉頭超出鏡頭範圍時,視角會凍結在最後位置。
- **自動啟動沒觸發**:確認工作管理員裡 DCS 的行程名稱就是 `DCS.exe`(Settings 裡的名稱欄位可改)。
- **opentrack 有資料、DCS 仍沒反應**:先開 opentrack 再開 DCS(順序反了要重開 DCS);確認沒有同時開著 TrackIR 軟體。備案:在 opentrack 把輸出改成 vJoy,再到 DCS Controls → View 指派視角軸。
- Settings 裡的 **Connection check** 會逐項列出:鏡頭、UDP 封包數、opentrack、熱鍵、DCS。
- 命令列版仍可用:`python dcs_headtracker.py --help`、`--selftest`、`--demo`。
