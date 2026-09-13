; *** Inno Setup 6 Vietnamese Language File ***
; Compatible with Inno Setup 6.x Unicode / UTF-8

[LangOptions]
LanguageName=Tiếng Việt
LanguageID=$042a
LanguageCodePage=0
DialogFontName=Segoe UI
DialogFontSize=9

[Messages]
; --- Startup ---
SetupAppTitle=Cài đặt - %1
SetupWindowTitle=Cài đặt - %1
UninstallAppTitle=Gỡ cài đặt - %1
UninstallAppFullTitle=Gỡ cài đặt hoàn toàn %1

; --- Common buttons ---
ButtonBack=< &Quay lại
ButtonNext=&Tiếp tục >
ButtonInstall=&Cài đặt
ButtonOK=Đồng ý
ButtonCancel=Hủy bỏ
ButtonYes=&Có
ButtonYesToAll=Có cho &tất cả
ButtonNo=&Không
ButtonNoToAll=K&hông cho tất cả
ButtonFinish=&Hoàn tất
ButtonBrowse=&Duyệt...
ButtonWizardBrowse=D&uyệt...
ButtonNewFolder=&Tạo thư mục mới

; --- Wizard Common ---
WizardSelectDir=Chọn thư mục cài đặt
SelectDirDesc=Nơi nào trên máy tính bạn muốn cài đặt [name]?
SelectDirLabel3=Trình cài đặt sẽ cài đặt [name] vào thư mục sau.
SelectDirBrowseLabel=Để tiếp tục, hãy nhấn Tiếp tục. Nếu bạn muốn chọn thư mục khác, hãy nhấn Duyệt.
DiskSpaceGBLabel=Cần ít nhất %1 GB dung lượng trống trên đĩa.
DiskSpaceMBLabel=Cần ít nhất %1 MB dung lượng trống trên đĩa.
CannotInstallTo=Không thể cài đặt vào "%1". Vui lòng chọn một thư mục khác.
InvalidPath=Bạn phải nhập đường dẫn đầy đủ kèm tên ổ đĩa, ví dụ:%n%nC:\APP%n%nhoặc đường dẫn mạng dạng:%n%n\\server\share
InvalidDrive=Ổ đĩa hoặc đường dẫn mạng bạn đã chọn không tồn tại hoặc không thể truy cập. Vui lòng chọn lại.
DiskSpaceWarningTitle=Không đủ dung lượng ổ đĩa
DiskSpaceWarning=Trình cài đặt cần ít nhất %1 KB dung lượng trống, nhưng ổ đĩa đã chọn chỉ còn %2 KB khả dụng.%n%nBạn có muốn tiếp tục không?
DirNameTooLong=Tên thư mục hoặc đường dẫn quá dài.
InvalidDirName=Tên thư mục không hợp lệ.
BadDirName32=Tên thư mục không được chứa bất kỳ ký tự nào sau đây:%n%n%1
DirExists=Thư mục:%n%n%1%n%nđã tồn tại. Bạn vẫn muốn cài đặt vào thư mục này chứ?
DirDoesntExist=Thư mục:%n%n%1%n%nchưa tồn tại. Bạn có muốn tạo thư mục này không?

; --- Wizard Tasks ---
WizardSelectTasks=Chọn tác vụ bổ sung
SelectTasksDesc=Những tác vụ bổ sung nào bạn muốn thực hiện?
SelectTasksLabel2=Chọn các tác vụ bổ sung bạn muốn trình cài đặt thực hiện trong quá trình cài đặt [name], sau đó nhấn Tiếp tục.
CreateDesktopIcon=Tạo biểu tượng ngoài &Màn hình chính (Desktop)
CreateQuickLaunchIcon=Tạo biểu tượng trên thanh &Quick Launch
AdditionalIcons=Biểu tượng bổ sung:

; --- Wizard Ready to Install ---
WizardReady=Sẵn sàng cài đặt
ReadyLabel1=Trình cài đặt đã sẵn sàng bắt đầu cài đặt [name] lên máy tính của bạn.
ReadyLabel2a=Nhấn Cài đặt để bắt đầu, hoặc nhấn Quay lại nếu bạn muốn xem lại hoặc thay đổi bất kỳ thiết lập nào.
ReadyLabel2b=Nhấn Cài đặt để bắt đầu cài đặt.

; --- Wizard Installing ---
WizardPreparing=Đang chuẩn bị cài đặt
PreparingDesc=Trình cài đặt đang chuẩn bị cài đặt [name] trên máy tính của bạn.
PreviousInstallDetected=Phát hiện phiên bản cũ đã được cài đặt. Trình cài đặt sẽ tự động cập nhật lên phiên bản mới.
WizardInstalling=Đang cài đặt
InstallingDesc=Vui lòng chờ trong khi trình cài đặt cài đặt [name] trên máy tính của bạn.

; --- Wizard Finished ---
FinishedHeadingLabel=Hoàn tất cài đặt [name]
FinishedLabelNoIcons=[name] đã được cài đặt thành công trên máy tính của bạn.
FinishedLabel=[name] đã được cài đặt thành công trên máy tính của bạn. Ứng dụng có thể được khởi chạy bằng cách chọn các biểu tượng đã cài đặt.
ClickFinish=Nhấn Hoàn tất để thoát khỏi trình cài đặt.
LaunchProgram=Khởi chạy %1 ngay bây giờ

; --- Program exit/cancel ---
ExitSetupTitle=Thoát cài đặt
ExitSetupMessage=Cài đặt chưa hoàn tất. Nếu bạn thoát ngay bây giờ, chương trình sẽ không được cài đặt.%n%nBạn có thể chạy lại trình cài đặt vào lúc khác để hoàn tất quá trình.%n%nBạn có chắc chắn muốn thoát khỏi trình cài đặt không?

; --- Uninstallation ---
ConfirmUninstall=Bạn có chắc chắn muốn gỡ bỏ hoàn toàn %1 và tất cả các thành phần của nó không?
UninstallStatusLabel=Vui lòng chờ trong khi %1 được gỡ bỏ khỏi máy tính của bạn.
UninstalledAll=%1 đã được gỡ cài đặt thành công khỏi máy tính của bạn.
UninstalledMost=Quá trình gỡ cài đặt %1 đã hoàn tất.%n%nMột số phần tử không thể gỡ bỏ được. Bạn có thể xóa chúng theo cách thủ công.
UninstalledAndNeedsRestart=Để hoàn tất việc gỡ cài đặt %1, máy tính của bạn phải được khởi động lại.%n%nBạn có muốn khởi động lại ngay bây giờ không?
UninstallDataNotice=Lưu ý: Dữ liệu người dùng (dự án, video đã xuất) lưu ngoài thư mục cài đặt sẽ không bị xóa.
