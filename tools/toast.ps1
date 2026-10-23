<#
  Windows toast helper. Reads title/body from UTF-8 files to avoid encoding loss.
  Exits 0 on success, 1 on failure (caller only logs).
#>
param(
  [Parameter(Mandatory = $true)][string]$TitleFile,
  [Parameter(Mandatory = $true)][string]$BodyFile
)

try {
  $title = (Get-Content -LiteralPath $TitleFile -Encoding UTF8 -Raw).Trim()
  $body = (Get-Content -LiteralPath $BodyFile -Encoding UTF8 -Raw).Trim()

  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

  $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
  $texts = $template.GetElementsByTagName('text')
  $texts.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
  $texts.Item(1).AppendChild($template.CreateTextNode($body)) | Out-Null

  $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Kurisu Persona').Show($toast)
  exit 0
} catch {
  exit 1
}
