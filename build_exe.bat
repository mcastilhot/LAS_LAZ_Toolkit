@echo off
rem ==========================================================================
rem  Gera o executavel do Ferramentas_LAZ (Windows 10/11 x64)
rem  Permite compilar em PASTA (--onedir, recomendado) ou ARQUIVO UNICO (--onefile)
rem ==========================================================================
setlocal
cd /d "%~dp0"

echo ==========================================================================
echo  LAS / LAZ Toolkit - Construtor de Executavel (PyInstaller)
echo ==========================================================================
echo.
echo Escolha o tipo de executavel a gerar:
echo   [1] Em Pasta / onedir (Recomendado - abre rapido e mais confiavel com GDAL/SciPy)
echo   [2] Arquivo Unico / onefile (.exe unico isolado)
echo.
set /p TIPO="Digite a opcao (1 ou 2) [padrao=1]: "
if "%TIPO%"=="" set TIPO=1

if "%TIPO%"=="2" (
    set SPEC=Ferramentas_LAZ_OneFile.spec
    set MODO=Arquivo Unico
) else (
    set SPEC=Ferramentas_LAZ.spec
    set MODO=Pasta
)

echo.
echo [INFO] Modo selecionado: %MODO% (%SPEC%)

rem 1) Ativa o ambiente virtual (.venv ou venv_build)
if exist ".venv\Scripts\activate.bat" (
    echo [INFO] Utilizando ambiente virtual existente (.venv)...
    call .venv\Scripts\activate.bat
) else if exist "venv_build\Scripts\activate.bat" (
    echo [INFO] Utilizando ambiente virtual existente (venv_build)...
    call venv_build\Scripts\activate.bat
) else (
    echo [INFO] Criando novo ambiente virtual venv_build...
    py -3.12 -m venv venv_build || py -3.11 -m venv venv_build || python -m venv venv_build
    if not exist "venv_build\Scripts\activate.bat" (
        echo [ERRO] Falha ao criar o ambiente virtual. Verifique a instalacao do Python 64-bit.
        pause
        exit /b 1
    )
    call venv_build\Scripts\activate.bat
)

rem 2) Atualiza pip e instala dependencias
echo.
echo [INFO] Verificando dependencias do requirements.txt...
python -m pip install --upgrade pip
pip install -r requirements.txt

rem 3) Compilacao com PyInstaller usando a spec escolhida
echo.
echo [INFO] Compilando com PyInstaller (%SPEC%)...
pyinstaller --noconfirm --clean %SPEC%

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERRO] Falha na compilacao com PyInstaller.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==========================================================================
echo  Compilacao concluida com sucesso!
if "%TIPO%"=="2" (
    echo  Executavel unico gerado em: dist\Ferramentas_LAZ_OneFile.exe
) else (
    echo  Executavel gerado em: dist\Ferramentas_LAZ\Ferramentas_LAZ.exe
    echo  (Lembre-se de copiar a pasta inteira "dist\Ferramentas_LAZ")
)
echo ==========================================================================
echo.
pause
