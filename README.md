# ZeroTier Alternative UI

Małe, alternatywne okienko dla ZeroTier One na Windows – zamiast menu schowanego
w ikonie przy zegarku dostajesz zwykłe okno z listą wszystkich sieci i informacją,
czy jesteś połączony.

Napisane w Pythonie z użyciem wbudowanego Tkinter – **bez żadnych dodatkowych
bibliotek**. Rozmawia bezpośrednio z lokalną usługą ZeroTier One przez jej
HTTP API (port 9993), więc działa równolegle z oficjalnym klientem.

## Co pokazuje

* adres Twojego węzła, wersję ZeroTier i status ONLINE/OFFLINE,
* tabelę sieci: nazwa, ID, status (Połączono / Brak autoryzacji / Pobieranie
  konfiguracji…), przydzielony adres IP, typ (Public/Private),
* kolory: zielony = połączono, pomarańczowy = w trakcie, czerwony = problem,
* licznik sieci i połączonych w dolnej linii.

## Co pozwala zrobić

* dołączyć do sieci (wpisz 16‑znakowe ID i kliknij **Dołącz** lub Enter),
* opuścić zaznaczoną sieć (**Opuść**),
* prawy przycisk myszy na sieci: szczegóły, kopiowanie ID / adresu IP,
  przełączanie opcji `allowManaged`, `allowGlobal`, `allowDefault`, `allowDNS`,
  opuszczenie sieci,
* dwuklik na sieci – okno szczegółów (interfejs, MAC, MTU, trasy…),
* auto‑odświeżanie co 5 s (można wyłączyć), opcja „Zawsze na wierzchu”.

## Wymagania

* Windows z zainstalowanym ZeroTier One (usługa musi działać),
* Python 3.9+ (z Tkinter – standardowy instalator z python.org go zawiera).

## Uruchomienie

```bat
python zerotier_ui.py
```

albo dwuklik na `ZeroTierUI.bat` (uruchamia przez `pythonw`, bez okna konsoli).

### Pierwsze uruchomienie i uprawnienia

Usługa ZeroTier wymaga tokenu z pliku `authtoken.secret`. Program szuka go w:

1. `%LOCALAPPDATA%\ZeroTier\authtoken.secret` (kopia w profilu użytkownika),
2. `%LOCALAPPDATA%\ZeroTier\One\authtoken.secret`,
3. `C:\ProgramData\ZeroTier\One\authtoken.secret` (plik usługi – wymaga
   uprawnień administratora),
4. zmiennej środowiskowej `ZT_AUTHTOKEN`.

Jeśli żadnej kopii w profilu nie ma, program zaproponuje jednorazowe
uruchomienie jako administrator. Wtedy skopiuje token do
`%LOCALAPPDATA%\ZeroTier\authtoken.secret` i kolejne uruchomienia nie będą już
wymagały podniesionych uprawnień.

Jeśli usługa działa na innym porcie niż 9993, ustaw zmienną `ZT_PORT`
(program czyta też plik `zerotier-one.port` z katalogu usługi).

## Budowanie pliku .exe (opcjonalnie)

```bat
pip install pyinstaller
pyinstaller --onefile --windowed --name ZeroTierUI zerotier_ui.py
```

Gotowy plik znajdzie się w `dist\ZeroTierUI.exe`.

## Działa też na Linux / macOS

Ścieżki tokenu są dobierane automatycznie (`/var/lib/zerotier-one`,
`/Library/Application Support/ZeroTier/One`, `~/.zeroTierOneAuthToken`).
