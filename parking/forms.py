from django import forms
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
import re
from .models import (
    Ticket,
    VehicleType,
    Customer,
    RatePlan,
    MonthlyPlate,
    ElectronicInvoiceOutbox,WorkType
)
# forms.py

class EInvoiceOutboxForm(forms.ModelForm):
    class Meta:
        model = ElectronicInvoiceOutbox
        fields = [
            "id_number",
            "full_name",
            "email",
            "total_amount_cop",
            # agrega/quita según tu modelo
            # "notes", "address", "phone", etc...
        ]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "id_number": forms.TextInput(attrs={"class": "form-control"}),
            "total_amount_cop": forms.NumberInput(attrs={"class": "form-control"}),
        }

class MonthlyChargeForm(forms.Form):
    METHOD_CHOICES = [
        ("", "— Selecciona una opción —"),
        ("CASH", "Efectivo"),
        ("TRANSFER", "Transferencia"),
    ]
    INVOICE_CHOICES = [
        ("", "— Selecciona una opción —"),
        ("NO", "No"),
        ("YES", "Sí"),
    ]

    method = forms.ChoiceField(choices=METHOD_CHOICES, required=True)
    invoice_required = forms.ChoiceField(choices=INVOICE_CHOICES, required=True)

class OperarioForm(forms.ModelForm):
    """
    Crea / edita operarios usando el User estándar de Django.
    - username: usuario de login (único)
    - first_name / last_name: nombre real
    - is_active: activo/inactivo
    - is_staff/is_superuser: SIEMPRE False (no admins aquí)
    """
    password1 = forms.CharField(
        label="Contraseña",
        widget=forms.PasswordInput,
        required=False
    )
    password2 = forms.CharField(
        label="Confirmar contraseña",
        widget=forms.PasswordInput,
        required=False
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Mensajes en español
        self.fields["username"].error_messages = {"required": "Ingresa un usuario."}
        self.fields["first_name"].error_messages = {"required": "Ingresa el nombre."}

        self.fields["username"].label = "Usuario (login)"
        self.fields["first_name"].label = "Nombre"
        self.fields["last_name"].label = "Apellido"
        self.fields["is_active"].label = "Activo"

        # Reglas
        self.fields["username"].required = True
        self.fields["first_name"].required = True
        self.fields["last_name"].required = False

        # ✅ Si estamos editando, no dejamos cambiar username (recomendado)
        if getattr(self.instance, "pk", None):
            self.fields["username"].disabled = True
            self.fields["password1"].required = False
            self.fields["password2"].required = False
        else:
            self.fields["password1"].required = True
            self.fields["password2"].required = True

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            raise forms.ValidationError("Ingresa un usuario.")

        if not getattr(self.instance, "pk", None):
            if User.objects.filter(username__iexact=username).exists():
                raise forms.ValidationError(
                    "Ese usuario ya existe. Usa otro o edítalo desde el listado."
                )
        return username

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""

        # En creación: ambas obligatorias
        if not getattr(self.instance, "pk", None):
            if not p1 or not p2:
                raise forms.ValidationError("Debes ingresar y confirmar la contraseña.")

        # Si se ingresó algo, deben coincidir
        if (p1 or p2) and p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")

        # Validación de password (si aplica) + traducción por CÓDIGO
        if p1:
            try:
                validate_password(
                    p1,
                    self.instance if getattr(self.instance, "pk", None) else None
                )
            except ValidationError as e:
                code_map = {
                    "password_too_short": "La contraseña es muy corta. Debe tener al menos 8 caracteres.",
                    "password_entirely_numeric": "La contraseña no puede ser únicamente numérica.",
                    "password_too_common": "La contraseña es demasiado común.",
                    "password_too_similar": "La contraseña es demasiado similar a tus datos personales.",
                }

                msgs = []
                for err in getattr(e, "error_list", []):
                    msg = code_map.get(getattr(err, "code", ""), err.message)
                    msgs.append(msg)

                if not msgs:
                    msgs = ["La contraseña no cumple las políticas de seguridad."]

                raise forms.ValidationError(msgs)

        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)

        # ✅ Asegurar que sean operarios (no staff)
        user.is_staff = False
        user.is_superuser = False

        p1 = self.cleaned_data.get("password1")
        if p1:
            user.set_password(p1)

        if commit:
            user.save()
        return user


class OperarioPasswordForm(forms.Form):
    password1 = forms.CharField(label="Nueva contraseña", widget=forms.PasswordInput, required=True)
    password2 = forms.CharField(label="Confirmar contraseña", widget=forms.PasswordInput, required=True)

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1") or ""
        p2 = cleaned.get("password2") or ""
        if p1 != p2:
            raise forms.ValidationError("Las contraseñas no coinciden.")
        try:
            validate_password(p1)
        except ValidationError as e:
            raise forms.ValidationError(list(e.messages))
        return cleaned


class VehicleTypeForm(forms.ModelForm):
    class Meta:
        model = VehicleType
        fields = ["name", "active"]
        error_messages = {
            "name": {
                "unique": "Ya existe un tipo de vehículo con ese nombre.",
                "required": "El nombre es obligatorio.",
            }
        }

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("El nombre es obligatorio.")
        return name


class RatePlanForm(forms.ModelForm):
    """
    Ajustado para nuevo RatePlan:
    - PARKING: permite HOUR y NIGHT
    - WORKSHOP: solo NIGHT
    - day_type: NORMAL / SUNDAY / HOLIDAY
    """
    class Meta:
        model = RatePlan
        fields = ["vehicle_type", "client_kind", "billing_unit", "day_type", "price_cop", "active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ✅ requerido
        self.fields["client_kind"].required = True
        self.fields["billing_unit"].required = True
        self.fields["day_type"].required = True

        # ✅ placeholders
        # client_kind
        ck_choices = list(self.fields["client_kind"].choices)
        if not ck_choices or ck_choices[0][0] != "":
            self.fields["client_kind"].choices = [("", "— Selecciona —")] + ck_choices

        # billing_unit
        bu_choices = list(self.fields["billing_unit"].choices)
        if not bu_choices or bu_choices[0][0] != "":
            self.fields["billing_unit"].choices = [("", "— Selecciona —")] + bu_choices

        # day_type
        dt_choices = list(self.fields["day_type"].choices)
        if not dt_choices or dt_choices[0][0] != "":
            self.fields["day_type"].choices = [("", "— Selecciona —")] + dt_choices

        # ✅ En creación (form sin POST y sin instancia), forzar que arranque vacío
        if not self.is_bound and not getattr(self.instance, "pk", None):
            self.fields["client_kind"].initial = ""
            self.fields["billing_unit"].initial = ""
            self.fields["day_type"].initial = ""

        # ✅ mensajes en español
        self.fields["client_kind"].error_messages = {"required": "Selecciona un tipo de cliente."}
        self.fields["vehicle_type"].error_messages = {"required": "Selecciona un tipo de vehículo."}
        self.fields["billing_unit"].error_messages = {"required": "Selecciona una unidad de cobro."}
        self.fields["day_type"].error_messages = {"required": "Selecciona el tipo de día (Normal/Domingo/Festivo)."}
        self.fields["price_cop"].error_messages = {"required": "Ingresa un precio válido."}

    def clean_client_kind(self):
        val = self.cleaned_data.get("client_kind")
        if val in (None, "", "---------"):
            raise forms.ValidationError("Selecciona un tipo de cliente.")
        return val

    def clean_billing_unit(self):
        val = self.cleaned_data.get("billing_unit")
        if val in (None, "", "---------"):
            raise forms.ValidationError("Selecciona una unidad de cobro.")
        return val

    def clean_day_type(self):
        val = self.cleaned_data.get("day_type")
        if val in (None, "", "---------"):
            raise forms.ValidationError("Selecciona el tipo de día (Normal/Domingo/Festivo).")
        return val

    def clean(self):
        cleaned = super().clean()

        ck = (cleaned.get("client_kind") or "").strip().upper()
        bu = (cleaned.get("billing_unit") or "").strip().upper()

        # Si aún no seleccionan algo, no forzamos más para no duplicar errores
        if not ck or not bu:
            return cleaned

        # ✅ Reglas nuevas:
        # WORKSHOP -> solo NIGHT
        if ck == "WORKSHOP" and bu != "NIGHT":
            self.add_error("billing_unit", "Para Taller la unidad debe ser 'Noche' (NIGHT).")

        # PARKING -> HOUR o NIGHT (válido)
        if ck == "PARKING" and bu not in ("HOUR", "NIGHT"):
            self.add_error("billing_unit", "Para Parqueadero la unidad debe ser 'Hora / Fracción' o 'Noche'.")

        return cleaned


@transaction.atomic
def save_rateplan_upsert(request, editing_rateplan=None):
    form = RatePlanForm(request.POST, instance=editing_rateplan)

    # Si está editando explícitamente, guarda normal
    if editing_rateplan:
        if form.is_valid():
            form.save()
            messages.success(request, "Tarifa actualizada correctamente.")
            return redirect("?mode=rateplans")
        messages.error(request, "No se pudo guardar. Revisa los campos.")
        return None, form

    # Crear/Actualizar automático
    if not form.is_valid():
        messages.error(request, "No se pudo guardar. Revisa los campos.")
        return None, form

    cd = form.cleaned_data
    vehicle_type = cd["vehicle_type"]
    client_kind = cd["client_kind"]
    billing_unit = cd["billing_unit"]
    day_type = cd.get("day_type")

    # ✅ unicidad nueva:
    rp = RatePlan.objects.filter(
        vehicle_type=vehicle_type,
        client_kind=client_kind,
        billing_unit=billing_unit,
        day_type=day_type,
    ).first()

    if rp:
        rp.price_cop = cd["price_cop"]
        rp.active = cd["active"]
        rp.save(update_fields=["price_cop", "active"])
        messages.success(request, "Ya existía esta tarifa. Se actualizó el precio/estado automáticamente.")
        return redirect("?mode=rateplans")

    # Si no existía, crea
    form.save()
    messages.success(request, "Tarifa creada correctamente.")
    return redirect("?mode=rateplans")


# =========================
# ✅ AJUSTE SOLICITADO: CustomerForm
# - Si es empresa: puede marcar "habilitada para crédito" (acumulable)
# - Si NO es empresa: siempre crédito=False
# - Si es empresa y crédito=False => automáticamente es "pago pendiente" (no requiere otro campo)
# =========================
class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        # ✅ Se agrega credit_enabled SIN tocar lo demás
        fields = ["id_number", "full_name", "email", "is_company", "credit_enabled"]
        labels = {
            "id_number": "Cédula o NIT",
            "full_name": "Nombre Completo o Razón Social",
            "email": "Correo Electrónico",
            "is_company": "¿Es empresa?",
            "credit_enabled": "Habilitada para crédito (acumulable)",
        }
        widgets = {
            "id_number": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ej: 1053890200"}),
            "full_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ej: Juan Pérez"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "correo@ejemplo.com"}),
            "is_company": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "credit_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned = super().clean()
        is_company = bool(cleaned.get("is_company"))

        # ✅ Si NO es empresa, no puede tener crédito (queda en False siempre)
        if not is_company:
            cleaned["credit_enabled"] = False

        return cleaned


class TicketCreateForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["plate", "vehicle_type", "client_kind"]
        labels = {
            "plate": "Placa",
            "vehicle_type": "Tipo vehículo",
            "client_kind": "Tipo cliente",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["vehicle_type"].queryset = VehicleType.objects.filter(active=True)
        self.fields["vehicle_type"].required = True
        self.fields["client_kind"].required = True
        self.fields["vehicle_type"].empty_label = "Seleccione tipo vehículo..."

        self.fields["client_kind"].choices = [("", "Seleccione tipo cliente...")] + list(
            getattr(Ticket, "CLIENT_KIND_CHOICES", [])
        )

    def clean_plate(self):
        raw = (self.cleaned_data.get("plate") or "").strip().upper()

        # Normalización igual que MonthlyPlateForm (y además quita cualquier símbolo raro)
        plate = raw.replace(" ", "").replace("-", "")
        plate = re.sub(r"[^A-Z0-9]", "", plate)

        if not plate:
            raise forms.ValidationError("La placa es obligatoria.")

        # 1) ❌ Bloquear si es placa mensual
        mp_qs = MonthlyPlate.objects.all()

        # Filtrar active=True SOLO si existe el campo
        try:
            MonthlyPlate._meta.get_field("active")
            mp_qs = mp_qs.filter(active=True)
        except Exception:
            pass

        # ✅ primer intento: match directo (si está normalizada como tu form mensual)
        if mp_qs.filter(plate__iexact=plate).exists():
            raise forms.ValidationError("PLACA MENSUAL")

        # ✅ respaldo: por si hay mensuales viejas guardadas con formato raro
        monthly_set = set(
            re.sub(r"[^A-Z0-9]", "", (p or "").strip().upper().replace(" ", "").replace("-", ""))
            for p in mp_qs.values_list("plate", flat=True)
        )
        if plate in monthly_set:
            raise forms.ValidationError("PLACA MENSUAL")

        # 2) ❌ Bloquear si ya existe ACTIVE (PENDING sí se permite)
        if Ticket.objects.filter(plate__iexact=plate, status="ACTIVE").exists():
            raise forms.ValidationError("PLACA ACTIVA")

        return plate



class EditActiveTicketForm(forms.ModelForm):
    """
    Para tickets en estado ACTIVE:
    - Permite editar SOLO: placa, tipo de vehículo, tipo de cliente (fijo).
    """
    class Meta:
        model = Ticket
        fields = ["plate", "vehicle_type", "client_kind"]
        labels = {
            "plate": "Placa",
            "vehicle_type": "Tipo vehículo",
            "client_kind": "Tipo cliente",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["vehicle_type"].queryset = VehicleType.objects.filter(active=True)

        self.fields["vehicle_type"].required = True
        self.fields["client_kind"].required = True

        self.fields["vehicle_type"].empty_label = "Seleccione tipo vehículo..."

        self.fields["client_kind"].choices = [("", "Seleccione tipo cliente...")] + list(
            getattr(Ticket, "CLIENT_KIND_CHOICES", [])
        )

class ClosePaymentForm(forms.Form):

    ticket_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    plate = forms.CharField(max_length=12, label="Placa")

    method = forms.ChoiceField(
        label="Método de operación",
        choices=[
            ("", "Seleccione método..."),
            ("CASH", "Efectivo (paga y cierra)"),
            ("TRANSFER", "Transferencia (paga y cierra)"),
            ("CREDIT", "Asignar a empresa (pendiente)"),
        ],
        required=True,
    )

    work_type = forms.ModelChoiceField(
        label="Tipo de trabajo",
        queryset=WorkType.objects.filter(active=True).order_by("name"),
        required=False,  # ✅ permite NONE (vacío)
        empty_label="NONE (sin servicio)",  # ✅ se verá como opción
    )



    work_amount_cop = forms.IntegerField(
        label="Valor trabajo (COP)",
        required=False,
        min_value=0,
        help_text="Solo aplica si seleccionas un tipo de trabajo.",
    )

    transfer_ref = forms.CharField(
        max_length=80,
        required=False,
        label="Referencia/Transacción",
    )

    company = forms.ModelChoiceField(
        label="Empresa",
        queryset=Customer.objects.filter(active=True, is_company=True),
        required=False,
        empty_label="Seleccione empresa...",
    )

    invoice_required = forms.ChoiceField(
        label="¿Factura electrónica?",
        choices=[("", "Seleccione..."), ("NO", "No"), ("YES", "Sí")],
        required=False,
    )

    id_number = forms.CharField(max_length=30, required=False, label="Cédula o NIT")
    full_name = forms.CharField(max_length=160, required=False, label="Nombres y apellidos / Razón social")
    email = forms.CharField(required=False, label="Correo electrónico")

    def clean(self):
        cleaned = super().clean()

        method = (cleaned.get("method") or "").strip()
        transfer_ref = (cleaned.get("transfer_ref") or "").strip()
        company = cleaned.get("company")

        invoice_required = (cleaned.get("invoice_required") or "").strip()
        id_number = (cleaned.get("id_number") or "").strip()
        full_name = (cleaned.get("full_name") or "").strip()
        email = (cleaned.get("email") or "").strip()

        if not method:
            raise forms.ValidationError("Debes seleccionar un método de operación.")

        ticket_id = cleaned.get("ticket_id")
        if ticket_id and method == "CREDIT":
            raise forms.ValidationError(
                "Un ticket PENDIENTE no puede asignarse a empresa desde este botón. Usa efectivo o transferencia."
            )

        # =========================
        # ✅ Trabajo adicional (FK -> string code)
        # =========================
        wt_obj = cleaned.get("work_type")  # WorkType o None
        wt_code = "NONE"
        if wt_obj:
            wt_code = (getattr(wt_obj, "code", "") or "").strip().upper() or "NONE"

        work_amount = cleaned.get("work_amount_cop")
        if work_amount in (None, ""):
            work_amount = 0
        try:
            work_amount = int(work_amount)
        except (TypeError, ValueError):
            raise forms.ValidationError("El valor del trabajo adicional debe ser un número entero (COP).")
        if work_amount < 0:
            raise forms.ValidationError("El valor del trabajo adicional no puede ser negativo.")

        # Si seleccionaron trabajo (no NONE) exige valor > 0
        if wt_code != "NONE":
            if work_amount <= 0:
                raise forms.ValidationError(
                    "Si seleccionas un tipo de trabajo, debes ingresar un valor adicional mayor a 0."
                )
        else:
            work_amount = 0

        # ✅ CLAVE: devolvemos STRING para que tu vista siga igual
        # work_type = (cleaned.get("work_type") or "NONE").strip()
        cleaned["work_type"] = wt_code
        cleaned["work_amount_cop"] = work_amount

        # =========================
        # Validación por método
        # =========================
        if method == "TRANSFER" and not transfer_ref:
            raise forms.ValidationError("Para transferencia debes ingresar la referencia o transacción.")

        if method == "CREDIT":
            if not company:
                raise forms.ValidationError("Para asignar a empresa debes seleccionar una empresa.")
            cleaned["invoice_required"] = "NO"

        if method != "TRANSFER":
            cleaned["transfer_ref"] = ""

        if method != "CREDIT":
            cleaned["company"] = None

        # Factura electrónica (solo CASH/TRANSFER)
        invoice_allowed = method in ("CASH", "TRANSFER")
        if invoice_allowed:
            if not invoice_required:
                raise forms.ValidationError("Debes seleccionar si requiere factura electrónica (Sí/No).")
        else:
            cleaned["invoice_required"] = "NO"
            invoice_required = "NO"

        if invoice_allowed and invoice_required == "YES":
            if not id_number:
                raise forms.ValidationError("Para factura electrónica debes ingresar la cédula o NIT.")

            existing = Customer.objects.filter(id_number=id_number, active=True).first()
            if existing:
                cleaned["_customer_obj"] = existing
                cleaned["full_name"] = ""
                cleaned["email"] = ""
            else:
                if not full_name:
                    raise forms.ValidationError("No existe ese documento. Ingresa nombres/razón social.")
                if not email:
                    raise forms.ValidationError("No existe ese documento. Ingresa un correo electrónico.")
                try:
                    validate_email(email)
                except ValidationError:
                    raise forms.ValidationError("El correo electrónico no es válido.")
                cleaned["_customer_obj"] = None
        else:
            cleaned["invoice_required"] = "NO" if method == "CREDIT" else invoice_required
            cleaned["id_number"] = ""
            cleaned["full_name"] = ""
            cleaned["email"] = ""
            cleaned["_customer_obj"] = None

        return cleaned



class CompanySettleForm(forms.Form):
    company = forms.ModelChoiceField(
        label="Empresa",
        queryset=Customer.objects.none(),   # ✅ se define en __init__
        empty_label="Seleccione empresa...",
        required=True,
    )

    method = forms.ChoiceField(
        label="Método de pago",
        choices=[
            ("", "Seleccione método..."),
            ("CASH", "Efectivo"),
            ("TRANSFER", "Transferencia"),
        ],
        required=True,
    )

    transfer_ref = forms.CharField(
        max_length=80,
        required=False,
        label="Referencia/Transacción",
    )

    invoice_required = forms.ChoiceField(
        label="¿Factura electrónica?",
        choices=[("", "Seleccione..."), ("NO", "No"), ("YES", "Sí")],
        required=True,
    )

    # ⚠️ Se mantienen por compatibilidad con tu template/POST,
    # pero YA NO se validan/usan en "company_settle" cuando invoice_required=YES.
    id_number = forms.CharField(max_length=30, required=False, label="Cédula o NIT")
    full_name = forms.CharField(max_length=160, required=False, label="Nombres y apellidos / Razón social")
    email = forms.CharField(required=False, label="Correo electrónico")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ✅ SOLO empresas habilitadas para crédito
        qs = Customer.objects.filter(active=True, is_company=True, credit_enabled=True)
        self.fields["company"].queryset = qs.order_by("full_name")

    def clean_company(self):
        company = self.cleaned_data.get("company")

        # ✅ defensa extra: si alguien manipula el POST
        if not company:
            raise forms.ValidationError("Debes seleccionar una empresa.")

        if not getattr(company, "active", False) or not getattr(company, "is_company", False):
            raise forms.ValidationError("Empresa no válida.")

        if not getattr(company, "credit_enabled", False):
            raise forms.ValidationError("Esta empresa no está habilitada para crédito.")

        return company

    def clean(self):
        cleaned = super().clean()

        company = cleaned.get("company")

        method = (cleaned.get("method") or "").strip()
        transfer_ref = (cleaned.get("transfer_ref") or "").strip()

        invoice_required = (cleaned.get("invoice_required") or "").strip()

        if not method:
            raise forms.ValidationError("Debes seleccionar un método de pago.")

        if method == "TRANSFER" and not transfer_ref:
            raise forms.ValidationError("Para transferencia debes ingresar la referencia o transacción.")

        if method != "TRANSFER":
            cleaned["transfer_ref"] = ""

        if not invoice_required:
            raise forms.ValidationError("Debes seleccionar si requiere factura electrónica (Sí/No).")

        # =========================================================
        # ✅ AJUSTE CLAVE:
        # En "Pago de Empresa", si invoice_required == YES:
        # - NO se pide id_number/full_name/email al usuario
        # - Se usa la info ya guardada en la empresa (company)
        # =========================================================
        if invoice_required == "YES":
            if not company:
                # por seguridad, aunque company es required y ya se valida
                raise forms.ValidationError("Debes seleccionar una empresa para facturar.")

            nit = (getattr(company, "id_number", "") or "").strip()
            name = (getattr(company, "full_name", "") or "").strip()
            mail = (getattr(company, "email", "") or "").strip()

            if not nit:
                raise forms.ValidationError("La empresa no tiene NIT guardado. Actualiza sus datos.")
            if not name:
                raise forms.ValidationError("La empresa no tiene nombre/razón social guardado. Actualiza sus datos.")
            if not mail:
                raise forms.ValidationError("La empresa no tiene correo guardado. Actualiza sus datos.")

            # valida formato de email (por si guardaron algo raro)
            try:
                validate_email(mail)
            except ValidationError:
                raise forms.ValidationError("El correo guardado de la empresa no es válido. Actualiza sus datos.")

            # llenamos cleaned con lo que imprimirá/guardará el backend
            cleaned["id_number"] = nit
            cleaned["full_name"] = name
            cleaned["email"] = mail

            # y el "customer" de factura es la misma empresa
            cleaned["_customer_obj"] = company

        else:
            cleaned["id_number"] = ""
            cleaned["full_name"] = ""
            cleaned["email"] = ""
            cleaned["_customer_obj"] = None

        return cleaned


class EditPaidServiceForm(forms.Form):
    """
    Editar trabajo/servicio en ticket PAID con opciones del modelo (WORK_TYPE_CHOICES).
    - Permite elegir NONE (en ese caso fuerza valor 0).
    """
    service_type = forms.ModelChoiceField(
        label="Tipo de servicio",
        queryset=WorkType.objects.filter(active=True).order_by("name"),
        required=False,
        empty_label="Ninguno",
    )

    service_amount_cop = forms.IntegerField(
        label="Valor del servicio (COP)",
        required=True,
        min_value=0,
    )

    def clean(self):
        cleaned = super().clean()
        obj = cleaned.get("service_type")  # WorkType o None
        amount = cleaned.get("service_amount_cop")

        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            raise forms.ValidationError(
                "El valor del servicio debe ser un número entero (COP)."
            )

        # Si no seleccionó nada (empty_label="Ninguno")
        if obj is None:
            cleaned["service_amount_cop"] = 0
            cleaned["service_type"] = None
            return cleaned

        # Si el WorkType tiene code = "NONE"
        if getattr(obj, "code", "").upper() == "NONE":
            cleaned["service_amount_cop"] = 0
            return cleaned

        # Si seleccionó servicio real, debe ser > 0
        if amount <= 0:
            raise forms.ValidationError(
                "Si seleccionas un servicio, el valor debe ser mayor a 0."
            )

        cleaned["service_amount_cop"] = amount
        return cleaned



class InspectForm(forms.Form):
    plate = forms.CharField(max_length=12, label="Placa")


class MonthlyPlateForm(forms.ModelForm):
    class Meta:
        model = MonthlyPlate
        fields = ["plate"]

    def clean_plate(self):
        p = (self.cleaned_data.get("plate") or "").strip().upper().replace(" ", "").replace("-", "")
        if not p.isalnum() or len(p) < 5:
            raise forms.ValidationError("Placa inválida.")
        return p