from .models import BusinessInfo

def business_info(request):
    return {
        "BUSINESS_INFO": BusinessInfo.objects.first()
    }
