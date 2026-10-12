# Indexador de publicaciones de Facebook

Este servicio revisa una página de Facebook y registra en SQLite las publicaciones que contienen enlaces a `mega.nz` o `mediafire.com`. **Solo indexa metadatos; nunca visita los enlaces externos ni descarga archivos.**

## Requisitos e instalación en Linux

Se requiere Python 3.10 o posterior. Playwright instala la versión de Chromium compatible con la versión de la librería. En un entorno virtual:

```bash
sudo apt update
sudo apt install -y python3 python3-venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" python -m playwright install --with-deps chromium
```

La instalación de Chromium y dependencias del sistema sigue el procedimiento oficial de [Playwright para Python](https://playwright.dev/python/docs/browsers).

## Ejecución

```bash
source .venv/bin/activate
python facebook_indexer.py \
  --url 'https://www.facebook.com/Allnewinfinity2.0/' \
  --page-name 'All New Infinity' \
  --database ./comics_index.db \
  --interval-minutes 30 \
  --scroll-rounds 8 \
  --whitelist Batman 'Spider-Man'
```

Sin `--whitelist`, usa `Batman` y `Spider-Man`. Si se proporciona `--whitelist` sin términos, la whitelist queda desactivada y todas las publicaciones encontradas empiezan con estado `pendiente`.

En este workspace de Windows, el entorno instalado puede ejecutarse desde PowerShell así:

```powershell
.\.venv\Scripts\python.exe .\facebook_indexer.py `
  --url 'https://www.facebook.com/Allnewinfinity2.0/' `
  --database .\comics_index.db `
  --interval-minutes 30 `
  --whitelist Batman 'Spider-Man'
```

Facebook puede requerir una sesión y cambia con frecuencia su DOM. Para una página que requiere acceso autenticado, genera el estado desde una sesión propia. El siguiente comando abre Chromium; inicia sesión manualmente y cierra la ventana para guardar el estado:

```bash
python -m playwright codegen --save-storage=./facebook-auth.json 'https://www.facebook.com/Allnewinfinity2.0/'
python facebook_indexer.py --storage-state ./facebook-auth.json
```

Protege ese JSON como una contraseña y no lo publiques ni lo subas al repositorio. El script no recibe ni almacena usuario o contraseña.

## Datos almacenados

- `posts`: título detectado, URL de la publicación, texto, fecha textual visible, estado de whitelist y marcas de tiempo.
- `post_links`: URL y dominio (`mega.nz` o `mediafire.com`) por publicación.
- `scan_runs`: resultado y conteos de cada revisión.

Los estados posibles de una publicación son `pendiente`, `ignorado_whitelist` y `error`. La fecha de publicación se conserva como texto tal como aparece en Facebook; `first_seen` y `last_seen` son timestamps UTC del indexador.

## Ejecutar con systemd

Ejemplo de unidad en `/etc/systemd/system/facebook-indexer.service` (ajusta usuario y rutas):

```ini
[Unit]
Description=Indexador de enlaces de publicaciones de Facebook
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=komga
WorkingDirectory=/opt/facebook-indexer
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/facebook-indexer/.playwright-browsers
ExecStart=/opt/facebook-indexer/.venv/bin/python /opt/facebook-indexer/facebook_indexer.py --url https://www.facebook.com/Allnewinfinity2.0/ --database /var/lib/facebook-indexer/comics_index.db --interval-minutes 30 --whitelist Batman Spider-Man
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Actívalo con:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now facebook-indexer.service
sudo journalctl -u facebook-indexer.service -f
```

La estructura del DOM de Facebook puede cambiar y bloquear o limitar automatización; si deja de exponer publicaciones como `role="article"`, habrá que actualizar el selector/extractor.
