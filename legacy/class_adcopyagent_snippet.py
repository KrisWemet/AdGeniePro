class AdCopyAgent:
    def run(self, product_info):
        return f"Boost your {product_info['product_name']} sales today!"

if __name__ == "__main__":
    product = {"product_name": "Fitness Tracker"}
    print(AdCopyAgent().run(product))
