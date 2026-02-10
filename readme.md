
---

# Parqueadero – Django + PostgreSQL

Sistema de parqueadero desarrollado en **Django** con base de datos **PostgreSQL**, orientado a operación diaria (ingreso y cobro) y administración de tarifas.

---

## Requisitos generales

* **Python 3.11**
* **Git**
* **PostgreSQL 16**
* Acceso a terminal

  * macOS: Terminal
  * Windows: PowerShell

---

## 1) Instalar Python 3.11

### macOS

Usando Homebrew:

```bash
brew update
brew install python@3.11
```

Verificar:

```bash
python3.11 --version
```

---

### Windows

1. Descargar desde:
   [https://www.python.org/downloads/release/python-311/](https://www.python.org/downloads/release/python-311/)

2. Durante la instalación:

   * ✅ Add Python to PATH
   * ✅ Install for all users

Verificar:

```powershell
python --version
```

---

## 2) Instalar Git

### macOS

```bash
brew install git
```

### Windows

Descargar desde:
[https://git-scm.com/download/win](https://git-scm.com/download/win)

Verificar (ambos sistemas):

```bash
git --version
```

---

## 3) Clonar el repositorio

### macOS / Windows

```bash
git clone https://github.com/Daprosero/parqueadero-django.git
cd parqueadero-django
```

---

## 4) Crear y activar entorno virtual

### macOS

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
```

---

### Windows

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python --version
```

Actualizar `pip` e instalar dependencias (ambos sistemas):

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## 5) Instalar PostgreSQL 16

### macOS

```bash
brew install postgresql@16
```

Agregar al PATH (Apple Silicon):

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Iniciar servicio:

```bash
brew services start postgresql@16
```

---

### Windows

1. Descargar desde:
   [https://www.postgresql.org/download/windows/](https://www.postgresql.org/download/windows/)

2. Durante la instalación:

   * Guardar contraseña del usuario `postgres`
   * Puerto por defecto: `5432`

Si `psql` no se reconoce, agregar al PATH:

```
C:\Program Files\PostgreSQL\16\bin
```

Verificar (ambos sistemas):

```bash
psql --version
```

---

## 6) Crear usuario y base de datos

### macOS / Windows

```bash
psql -U postgres
```

Ejecutar dentro de PostgreSQL:

```sql
CREATE USER parking_user WITH PASSWORD '123456';
CREATE DATABASE parking_db OWNER parking_user;
GRANT ALL PRIVILEGES ON DATABASE parking_db TO parking_user;
\q
```

---

## 7) Ejecutar migraciones

### macOS / Windows

```bash
python manage.py makemigrations
python manage.py migrate
```

---

## 8) Crear usuario administrador

### macOS / Windows

```bash
python manage.py createsuperuser
```

---

## 9) Ejecutar el servidor de desarrollo

### macOS / Windows

```bash
python manage.py runserver
```

---
