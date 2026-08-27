from django import forms
from food.models import FoodItem


class VendorFoodItemForm(forms.ModelForm):
    class Meta:
        model = FoodItem
        fields = [
            "name",
            "price",
            "description",
            "category",
            "image",
            "is_available",
        ]

        labels = {
            "name": "Food name",
            "price": "Price (₹)",
            "description": "Description",
            "category": "Category",
            "image": "Food image",
            "is_available": "Available",
        }

        widgets = {
            "name": forms.TextInput(attrs={
                "placeholder": "Example: Paneer Tikka Wrap"
            }),
            "price": forms.NumberInput(attrs={
                "placeholder": "120",
                "min": "0",
                "step": "0.01"
            }),
            "description": forms.Textarea(attrs={
                "placeholder": "Describe the food item"
            }),
        }
