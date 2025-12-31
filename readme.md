

# Parqueadero – Django + PostgreSQL (macOS)

Sistema básico de parqueadero desarrollado en Django, con:

* **Administrador**: gestión de tarifas y tipos de vehículo desde Django Admin.
* **Operario**: registro de ingreso y cobro de vehículos.

---

## Requisitos del sistema

* macOS
* Homebrew
* Python **3.11**
* PostgreSQL **16** (instalado con Homebrew)

---

## 0) Instalar Homebrew

Homebrew **no viene instalado por defecto en macOS**.
Ejecuta este comando **una sola vez** en la terminal:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Al finalizar, agrega Homebrew al `PATH` (Apple Silicon):

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

Verificar instalación:

```bash
brew --version
```

---

## 1) Instalar Python 3.11 con Homebrew

Actualizar Homebrew e instalar Python 3.11:

```bash
brew update
brew install python@3.11
```

Verificar instalación:

```bash
python3.11 --version
```

> Si el comando no es reconocido, abre una nueva terminal.

---

## 2) Clonar el repositorio

```bash
git clone https://github.com/Daprosero/parqueadero-django.git
cd Parqueadero
```

---

## 3) Crear y activar entorno virtual (Python 3.11)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
```

Actualizar `pip` e instalar dependencias:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## 4) Instalar PostgreSQL 16 con Homebrew

Instalar PostgreSQL:

```bash
brew install postgresql@16
```

Agregar `psql` al `PATH` (necesario en Macs con Apple Silicon):

```bash
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
psql --version
```

Iniciar el servicio de PostgreSQL:

```bash
brew services start postgresql@16
brew services list | grep postgres
```

---

## 5) Crear usuario y base de datos (una sola vez)

Entrar a PostgreSQL:

```bash
psql postgres
```

Dentro de `psql`, ejecutar:

```sql
CREATE USER parking_user WITH PASSWORD '123456';
CREATE DATABASE parking_db OWNER parking_user;
GRANT ALL PRIVILEGES ON DATABASE parking_db TO parking_user;
\q
```

---

## 6) Crear las tablas del sistema (migraciones)

```bash
python manage.py makemigrations
python manage.py migrate
```

---

## 7) Crear usuario administrador (superusuario)

```bash
python manage.py createsuperuser
```

---

## 8) Crear datos iniciales (tarifas y tipos de vehículo)

Ejecutar el comando de inicialización:

```bash
python manage.py seed_rates
```

Este comando:

* Crea tipos de vehículo (Moto, Carro, Camioneta)
* Crea tarifas iniciales con valores aleatorios
* Se utiliza solo para inicializar el sistema

---

## 9) Crear usuario Operario

Desde el panel de administración:

1. Ir a **Users → Add user**
2. Crear un usuario (ejemplo: `operario1`)
3. Configurar:

   * **Active**: ✅
   * **Staff status**: ❌
   * **Superuser status**: ❌

---

## 10) Ejecutar el servidor de desarrollo

```bash
python manage.py runserver
```

Rutas disponibles:

* **Administrador**
  [http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/)

* **Operario – Ingreso**
  [http://127.0.0.1:8000/operario/ingreso/](http://127.0.0.1:8000/operario/ingreso/)

* **Operario – Cobro**
  [http://127.0.0.1:8000/operario/cobro/](http://127.0.0.1:8000/operario/cobro/)

