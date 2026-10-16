# Substituidor de classes LAS/LAZ

Aplicacao Python para substituir classes/pontos em arquivos `.las` e `.laz`, com dois modos:

- **Geral**: aplica a troca no arquivo inteiro.
- **Por area**: aplica somente dentro de geometrias de um `.shp` ou `.dxf`, usando buffer em metros.

## Instalacao

Recomendado usar Python 3.10+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Interface grafica

```powershell
python -m las_class_replacer.gui
```

## Linha de comando

Substituir pontos da classe 2 do arquivo A pelos pontos classe 2 do arquivo B, somente dentro de um SHP/DXF com buffer de 20 m:

```powershell
python -m las_class_replacer.cli replace-points `
  --target A.laz `
  --source B.laz `
  --output A_substituido.laz `
  --target-class 2 `
  --source-class 2 `
  --boundary area.shp `
  --buffer 20
```

Trocar a classificacao de pontos classe 2 para classe 1 no arquivo inteiro:

```powershell
python -m las_class_replacer.cli relabel `
  --target A.laz `
  --output A_reclassificado.laz `
  --from-class 2 `
  --to-class 1
```

Processamento em lote por pares com mesmo nome de arquivo:

```powershell
python -m las_class_replacer.cli batch-replace `
  --target-dir pasta_A `
  --source-dir pasta_B `
  --output-dir saida `
  --target-class 2 `
  --source-class 2 `
  --boundary area.dxf `
  --buffer 10
```

## Observacoes importantes

- O buffer usa as unidades do sistema de coordenadas dos arquivos. Para buffer em metros, LAS/LAZ e SHP/DXF devem estar em CRS projetado em metros.
- Para DXF, a aplicacao le entidades `LWPOLYLINE`, `POLYLINE`, `LINE`, `CIRCLE`, `ARC` e `HATCH` quando possivel.
- Em `replace-points`, o resultado contem os pontos do arquivo A, removendo os pontos da classe escolhida dentro da area, e adiciona os pontos da classe escolhida do arquivo B dentro da mesma area.
# LAS / LAZ Toolkit

O executável Windows está em `dist/LAS_LAZ_Toolkit.exe` e foi compilado a partir de `las_laz_toolkit.py` usando `LAS_LAZ_Toolkit.spec`.

A versão atual inclui as ferramentas básicas LAS/LAZ (metadados, renomeação, edição e cópia de PRJ, correção de extensão e conversão de versão). As funções geoespaciais das abas 7–9 precisam de `scipy`, `rasterio`, `shapely`, `scikit-image` e `geopandas`; como essas bibliotecas não estavam instaladas no ambiente usado para esta compilação, as abas apresentam a mensagem de dependências ausentes.

Para reconstruir o executável, use o ambiente `.buildvenv`:

```powershell
.buildvenv\Scripts\python.exe -m PyInstaller --noconfirm --clean LAS_LAZ_Toolkit.spec
```
